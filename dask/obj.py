
from into import discover, convert, append
from toolz import merge, concat, partition
from datashape import DataShape
from datashape.dispatch import dispatch
from operator import add
import itertools
from math import ceil
from collections import Iterable
import operator
import numpy as np
from . import core, threaded
from .threaded import inline
from .array import (getem, concatenate, concatenate2, top,
    broadcast_dimensions)


class Array(object):
    """ Array object holding a dask """
    __slots__ = 'dask', 'name', 'shape', 'blockshape'

    def __init__(self, dask, name, shape, blockshape):
        self.dask = dask
        self.name = name
        self.shape = shape
        self.blockshape = blockshape

    @property
    def numblocks(self):
        return tuple(int(ceil(a / b))
                     for a, b in zip(self.shape, self.blockshape))

    def _get_block(self, *args):
        return core.get(self.dask, (self.name,) + args)

    @property
    def ndim(self):
        return len(self.shape)

    def keys(self, *args):
        if self.ndim == 0:
            return [(self.name,)]
        ind = len(args)
        if ind + 1 == self.ndim:
            return [(self.name,) + args + (i,)
                        for i in range(self.numblocks[ind])]
        else:
            return [self.keys(*(args + (i,)))
                        for i in range(self.numblocks[ind])]


def atop(func, out, out_ind, *args):
    """ Array object version of dask.array.top """
    arginds = list(partition(2, args)) # [x, ij, y, jk] -> [(x, ij), (y, jk)]
    numblocks = dict([(a.name, a.numblocks) for a, ind in arginds])
    argindsstr = list(concat([(a.name, ind) for a, ind in arginds]))

    dsk = top(func, out, out_ind, *argindsstr, numblocks=numblocks)

    # Dictionary mapping {i: 3, j: 4, ...} for i, j, ... the dimensions
    shapes = dict((a, a.shape) for a, _ in arginds)
    dims = broadcast_dimensions(arginds, shapes)
    blockshapes = dict((a, a.blockshape) for a, _ in arginds)
    blockdims = broadcast_dimensions(arginds, blockshapes)

    shape = tuple(dims[i] for i in out_ind)
    blockshape = tuple(blockdims[i] for i in out_ind)

    dsks = [a.dask for a, _ in arginds]
    return Array(merge(dsk, *dsks), out, shape, blockshape)


@discover.register(Array)
def discover_dask_array(a, **kwargs):
    block = a._get_block(*([0] * a.ndim))
    return DataShape(*(a.shape + (discover(block).measure,)))


arrays = [np.ndarray]
try:
    import h5py
    arrays.append(h5py.Dataset)
except ImportError:
    pass
try:
    import bcolz
    arrays.append(bcolz.carray)
except ImportError:
    pass


names = ('x_%d' % i for i in itertools.count(1))

@convert.register(Array, tuple(arrays), cost=0.01)
def array_to_dask(x, name=None, blockshape=None, **kwargs):
    name = name or next(names)
    dask = merge({name: x}, getem(name, blockshape, x.shape))

    return Array(dask, name, x.shape, blockshape)


@convert.register(np.ndarray, Array, cost=0.5)
def dask_to_numpy(x, get=threaded.get, **kwargs):
    dsk2 = inline(x.dask, fast_functions=set([operator.getitem, np.transpose]))
    return concatenate(get(dsk2, x.keys(), **kwargs))


@convert.register(float, Array, cost=0.5)
def dask_to_float(x, get=threaded.get, **kwargs):
    result = get(x.dask, x.keys(), **kwargs)
    while isinstance(result, Iterable):
        assert len(result) == 1
        result = result[0]
    return result


def insert_to_ooc(out, arr):
    from threading import Lock
    lock = Lock()
    def store(x, *args):
        with lock:
            ind = tuple([slice(i*d, (i+1)*d) for i, d in zip(args, arr.blockshape)])
            out[ind] = x
        return None

    name = 'store-%s' % arr.name
    return dict(((name,) + t[1:], (store, t) + t[1:]) for t in core.flatten(arr.keys()))


@append.register(tuple(arrays), Array)
def store_Array_in_ooc_data(out, arr, **kwargs):
    update = insert_to_ooc(out, arr)
    dsk = merge(arr.dask, update)

    # Resize output dataset to accept new data
    assert out.shape[1:] == arr.shape[1:]
    resize(out, out.shape[0] + arr.shape[0])  # elongate

    dsk2 = inline(dsk, fast_functions=set([operator.getitem, np.transpose]))
    threaded.get(dsk2, list(update.keys()), **kwargs)
    return out


@dispatch(bcolz.carray, int)
def resize(x, size):
    return x.resize(size)


@dispatch(h5py.Dataset, int)
def resize(x, size):
    s = list(x.shape)
    s[0] = size
    return resize(x, tuple(s))

@dispatch(h5py.Dataset, tuple)
def resize(x, shape):
    return x.resize(shape)

from blaze.dispatch import dispatch
from blaze.compute.core import compute_up
from blaze import compute, ndim
from blaze.expr import ElemWise, symbol, Reduction, Transpose, TensorDot, Expr
from toolz import curry, compose

def compute_it(expr, leaves, *data, **kwargs):
    kwargs.pop('scope')
    return compute(expr, dict(zip(leaves, data)), **kwargs)


def elemwise_array(expr, *data, **kwargs):
    leaves = expr._inputs
    expr_inds = tuple(range(ndim(expr)))[::-1]
    return atop(curry(compute_it, expr, leaves, **kwargs),
                next(names), expr_inds,
                *concat((dat, tuple(range(ndim(dat))[::-1])) for dat in data))

for i in range(10):
    compute_up.register(ElemWise, *([Array] * i))(elemwise_array)


from blaze.expr.split import split

@dispatch(Reduction, Array)
def compute_up(expr, data, **kwargs):
    leaf = expr._leaves()[0]
    chunk = symbol('chunk', DataShape(*(data.blockshape +
        (leaf.dshape.measure,))))
    (chunk, chunk_expr), (agg, agg_expr) = split(expr._child, expr, chunk=chunk)

    inds = tuple(range(ndim(leaf)))
    tmp = atop(curry(compute_it, chunk_expr, [chunk], **kwargs),
               next(names), inds,
               data, inds)

    return atop(compose(curry(compute_it, agg_expr, [agg], **kwargs),
                        curry(concatenate2, axes=expr.axis)),
                next(names), tuple(i for i in inds if i not in expr.axis),
                tmp, inds)


@dispatch(Transpose, Array)
def compute_up(expr, data, **kwargs):
    return atop(curry(np.transpose, axes=expr.axes),
                next(names), expr.axes,
                data, tuple(range(ndim(expr))))


alphabet = 'abcdefghijklmnopqrstuvwxyz'
ALPHABET = alphabet.upper()


@curry
def many(a, b, binop=None, reduction=None, **kwargs):
    """
    Apply binary operator to pairwise to sequences, then reduce.

    >>> many([1, 2, 3], [10, 20, 30], mul, sum)  # dot product
    140
    """
    return reduction(map(curry(binop, **kwargs), a, b))



@dispatch(TensorDot, Array, Array)
def compute_up(expr, lhs, rhs, **kwargs):
    left_index = list(alphabet[:ndim(lhs)])
    right_index = list(ALPHABET[:ndim(rhs)])
    out_index = left_index + right_index
    for l, r in zip(expr._left_axes, expr._right_axes):
        out_index.remove(right_index[r])
        out_index.remove(left_index[l])
        right_index[r] = left_index[l]

    func = many(binop=np.tensordot, reduction=sum,
                axes=(expr._left_axes, expr._right_axes))
    return atop(func,
                next(names), out_index,
                lhs, tuple(left_index),
                rhs, tuple(right_index))

