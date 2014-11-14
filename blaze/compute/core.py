from __future__ import absolute_import, division, print_function
import numbers
from datetime import date, datetime
import toolz
from toolz import first, concat, memoize, unique
import itertools
from collections import Iterator

from ..compatibility import basestring
from ..expr import Expr, Symbol, Symbol, eval_str
from ..dispatch import dispatch

__all__ = ['compute', 'compute_up']

base = (numbers.Real, basestring, date, datetime)


@dispatch(Expr, object)
def pre_compute(leaf, data, scope=None):
    """ Transform data prior to calling ``compute`` """
    return data


@dispatch(Expr, object)
def post_compute(expr, result, scope=None):
    """ Effects after the computation is complete """
    return result


@dispatch(Expr, object)
def optimize(expr, data):
    """ Optimize expression to be computed on data """
    return expr


@dispatch(object, object)
def compute_up(a, b, **kwargs):
    raise NotImplementedError("Blaze does not know how to compute "
                              "expression of type `%s` on data of type `%s`"
                              % (type(a).__name__, type(b).__name__))


@dispatch(base)
def compute_up(a, **kwargs):
    return a


@dispatch((list, tuple))
def compute_up(seq, scope=None, **kwargs):
    return type(seq)(compute(item, scope or {}, **kwargs) for item in seq)


@dispatch(Expr, object)
def compute(expr, o, **kwargs):
    """ Compute against single input

    Assumes that only one Symbol exists in expression

    >>> t = Symbol('t', 'var * {name: string, balance: int}')
    >>> deadbeats = t[t['balance'] < 0]['name']

    >>> data = [['Alice', 100], ['Bob', -50], ['Charlie', -20]]
    >>> # list(compute(deadbeats, {t: data}))
    >>> list(compute(deadbeats, data))
    ['Bob', 'Charlie']
    """
    ts = set([x for x in expr._subterms() if isinstance(x, Symbol)])
    if len(ts) == 1:
        return compute(expr, {first(ts): o}, **kwargs)
    else:
        raise ValueError("Give compute dictionary input, got %s" % str(o))


@dispatch(object)
def compute_down(expr):
    """ Compute the expression on the entire inputs

    inputs match up to leaves of the expression
    """
    return expr


def issubtype(a, b):
    """ A custom issubclass """
    if issubclass(a, b):
        return True
    if issubclass(a, (tuple, list, set)) and issubclass(b, Iterator):
        return True
    if issubclass(b, (tuple, list, set)) and issubclass(a, Iterator):
        return True
    return False

def type_change(old, new):
    """ Was there a significant type change between old and new data?

    >>> type_change([1, 2], [3, 4])
    False
    >>> type_change([1, 2], [3, [1,2,3]])
    True

    Some special cases exist, like no type change from list to Iterator

    >>> type_change([[1, 2]], [iter([1, 2])])
    False
    """
    if all(isinstance(x, base) for x in old + new):
        return False
    if len(old) != len(new):
        return True
    new_types = list(map(type, new))
    old_types = list(map(type, old))
    return not all(map(issubtype, new_types, old_types))


def top_then_bottom_then_top_again_etc(expr, scope, **kwargs):
    """

    Compute expression against scope

    Starts with compute_down from the top.  Then computes up from the leaves
    until a major type change.  Then tries again at the top, etc..

    >>> import numpy as np

    >>> s = Symbol('s', 'var * {name: string, amount: int}')
    >>> data = np.array([('Alice', 100), ('Bob', 200), ('Charlie', 300)],
    ...                 dtype=[('name', 'S7'), ('amount', 'i4')])

    >>> e = s.amount.sum() + 1
    >>> top_then_bottom_then_top_again_etc(e, {s: data})
    601
    """
    # Base case: expression is in dict, return associated data
    if expr in scope:
        return scope[expr]

    if not hasattr(expr, '_leaves'):
        return expr

    leaf_exprs = list(expr._leaves())
    leaf_data = [scope.get(leaf) for leaf in leaf_exprs]

    # See if we have a direct computation path with compute_down
    try:
        return compute_down(expr, *leaf_data, **kwargs)
    except NotImplementedError:
        pass

    # Compute from the bottom until there is a data type change
    new_expr, new_scope = bottom_up_until_type_break(expr, scope)

    # Re-optimize data and expressions
    optimize_ = kwargs.get('optimize', optimize)
    pre_compute_ = kwargs.get('pre_compute', pre_compute)
    if pre_compute_:
        new_scope2 = {e: pre_compute(new_expr, datum, scope=new_scope)
                        for e, datum in new_scope.items()}
        # leaf_data2 = [pre_compute_(expr, child) for child in new_leaf_data]
    else:
        new_scope2 = new_scope
    if optimize_:
        try:
            new_expr2 = optimize_(new_expr, *[new_scope2[leaf]
                                              for leaf in new_expr._leaves()])
        except NotImplementedError:
            new_expr2 = new_expr
    else:
        new_expr2 = new_expr

    # Repeat
    return top_then_bottom_then_top_again_etc(new_expr2, new_scope2)


def top_to_bottom(d, expr, **kwargs):
    """ Processes an expression top-down then bottom-up """
    # Base case: expression is in dict, return associated data
    if expr in d:
        return d[expr]

    if not hasattr(expr, '_leaves'):
        return expr

    leaves = list(expr._leaves())
    data = [d.get(leaf) for leaf in leaves]

    # See if we have a direct computation path with compute_down
    try:
        return compute_down(expr, *data, **kwargs)
    except NotImplementedError:
        pass

    optimize_ = kwargs.get('optimize', optimize)
    pre_compute_ = kwargs.get('pre_compute', pre_compute)

    # Otherwise...
    # Compute children of this expression
    if hasattr(expr, '_inputs'):
        children = [top_to_bottom(d, child, **kwargs)
                        for child in expr._inputs]
    else:
        children = []

    # Did we experience a data type change?
    if type_change(data, children):

        # If so call pre_compute again
        if pre_compute_:
            children = [pre_compute_(expr, child) for child in children]

        # If so call optimize again
        if optimize_:
            try:
                expr = optimize_(expr, *children)
            except NotImplementedError:
                pass

    # Compute this expression given the children
    return compute_up(expr, *children, scope=d, **kwargs)


_names = ('leaf_%d' % i for i in itertools.count(1))

_leaf_cache = dict()
_used_tokens = set()
def _reset_leaves():
    _leaf_cache.clear()
    _used_tokens.clear()

def makeleaf(expr):
    """ Name of a new leaf replacement for this expression


    >>> _reset_leaves()

    >>> t = Symbol('t', '{x: int, y: int, z: int}')
    >>> makeleaf(t)
    t
    >>> makeleaf(t.x)
    x
    >>> makeleaf(t.x + 1)
    x
    >>> makeleaf(t.x + 1)
    x
    >>> makeleaf(t.x).isidentical(makeleaf(t.x + 1))
    False

    >>> from blaze import sin, cos
    >>> x = Symbol('x', 'real')
    >>> makeleaf(cos(x)**2).isidentical(sin(x)**2)
    False
    """
    name = expr._name or '_'
    token = None
    if expr in _leaf_cache:
        return _leaf_cache[expr]
    if (name, token) in _used_tokens:
        for token in itertools.count():
            if (name, token) not in _used_tokens:
                break
    result = Symbol(name, expr.dshape, token)
    _used_tokens.add((name, token))
    _leaf_cache[expr] = result
    return result


def data_leaves(expr, scope):
    return [scope[leaf] for leaf in expr._leaves()]


def bottom_up_until_type_break(expr, scope):
    """

    Parameters
    ----------

    expr: Expression

    *data: Sequence of data corresponding to leaves

    Returns
    -------

    expr:
        New expression with lower subtrees replaced with leaves
    scope:
        New scope with entries for those leaves

    Examples
    --------

    >>> import numpy as np

    >>> s = Symbol('s', 'var * {name: string, amount: int}')
    >>> data = np.array([('Alice', 100), ('Bob', 200), ('Charlie', 300)],
    ...                 dtype=[('name', 'S7'), ('amount', 'i4')])

    This computation completes without changing type.  We get back a leaf
    symbol and a computational result

    >>> e = (s.amount + 1).distinct()
    >>> bottom_up_until_type_break(e, {s: data})
    (amount, {amount: array([101, 201, 301], dtype=int32)})

    This computation has a type change midstream, so we stop and get the
    unfinished computation.

    >>> e = s.amount.sum() + 1
    >>> bottom_up_until_type_break(e, {s: data})
    (amount_sum + 1, {amount_sum: 600})

    >>> x = Symbol('x', 'int')
    >>> bottom_up_until_type_break(x + x, {x: 1})  # empty string Symbol-name
    (_, {_: 2})
    """
    if expr in scope:
        leaf = makeleaf(expr)
        return leaf, {leaf: scope[expr]}

    inputs = list(unique(expr._inputs))

    exprs, new_scopes = zip(*[bottom_up_until_type_break(i, scope)
                             for i in inputs])
    # data = list(concat(data))
    new_scope = toolz.merge(new_scopes)
    new_expr = expr._subs(dict((i, e) for i, e in zip(inputs, exprs)
                                      if not i.isidentical(e)))

    old_expr_leaves = expr._leaves()
    old_data_leaves = [scope.get(leaf) for leaf in old_expr_leaves]

    # If the leaves have change substantially then stop
    if type_change(sorted(new_scope.values(), key=type),
                   sorted(old_data_leaves, key=type)):
        return new_expr, new_scope
    else:
        leaf = makeleaf(expr)
        _data = [new_scope[i] for i in new_expr._inputs]
        return leaf, {leaf: compute_up(new_expr, *_data, scope=new_scope)}


def bottom_up(d, expr):
    """
    Process an expression from the leaves upwards

    Parameters
    ----------

    d : dict mapping {Symbol: data}
        Maps expressions to data elements, likely at the leaves of the tree
    expr : Expr
        Expression to compute

    Helper function for ``compute``
    """
    # Base case: expression is in dict, return associated data
    if expr in d:
        return d[expr]

    # Compute children of this expression
    children = ([bottom_up(d, child) for child in expr._inputs]
                if hasattr(expr, '_inputs') else [])

    # Compute this expression given the children
    result = compute_up(expr, *children, scope=d)

    return result


def swap_resources_into_scope(expr, scope):
    """ Translate interactive expressions into normal abstract expressions

    Interactive Blaze expressions link to data on their leaves.  From the
    expr/compute perspective, this is a hack.  We push the resources onto the
    scope and return simple unadorned expressions instead.

    Example
    -------

    >>> from blaze import Data
    >>> t = Data([1, 2, 3], dshape='3 * int', name='t')
    >>> swap_resources_into_scope(t.head(2), {})
    (t.head(2), {t: [1, 2, 3]})
    """
    resources = expr._resources()
    symbol_dict = dict((t, Symbol(t._name, t.dshape)) for t in resources)
    resources = dict((symbol_dict[k], v) for k, v in resources.items())
    scope = toolz.merge(resources, scope)
    expr = expr._subs(symbol_dict)

    return expr, scope


@dispatch(Expr, dict)
def compute(expr, d, **kwargs):
    """ Compute expression against data sources

    >>> t = Symbol('t', 'var * {name: string, balance: int}')
    >>> deadbeats = t[t['balance'] < 0]['name']

    >>> data = [['Alice', 100], ['Bob', -50], ['Charlie', -20]]
    >>> list(compute(deadbeats, {t: data}))
    ['Bob', 'Charlie']
    """
    _reset_leaves()
    optimize_ = kwargs.get('optimize', optimize)
    pre_compute_ = kwargs.get('pre_compute', pre_compute)

    expr2, d2 = swap_resources_into_scope(expr, d)
    if pre_compute_:
        d3 = dict((e, pre_compute_(e, dat)) for e, dat in d2.items())
    else:
        d3 = d2

    if optimize_:
        try:
            expr3 = optimize_(expr2, *[v for e, v in d3.items() if e in expr2])
        except NotImplementedError:
            expr3 = expr2
    else:
        expr3 = expr2
    result = top_then_bottom_then_top_again_etc(expr3, d3, **kwargs)
    return post_compute(expr3, result, scope=d3)
