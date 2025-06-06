# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.
# pylint: disable=invalid-name
"""Default legalization function for manipulate operators."""
from typing import Optional

import tvm
from tvm import topi, tir, relax, te
from tvm.relax.op.base import call_tir
from tvm.relax.struct_info import TensorStructInfo
from tvm.relax.utils import gen_call_tir_inputs
from tvm.tir.expr import IntImm
from ...block_builder import BlockBuilder
from ...expr import Call, Expr, Var, Tuple, TupleGetItem, ShapeExpr
from .common import TEFunc, LegalizeFunc, register_legalize


def _reshape(
    te_func: TEFunc, primfunc_name: str, is_collapse_sum_like: bool = False
) -> LegalizeFunc:
    def reshape_call_te(bb: BlockBuilder, call: Call):
        tgt_shape = call.args[1].struct_info.shape if is_collapse_sum_like else call.args[1]
        # If target shape is Var, pass its bound expr only when it is ShapeExpr
        if isinstance(tgt_shape, Var):
            tgt_shape = bb.lookup_binding(tgt_shape)
            assert isinstance(tgt_shape, ShapeExpr)
        return bb.call_te(te_func, call.args[0], tgt_shape, primfunc_name_hint=primfunc_name)

    return reshape_call_te


register_legalize("relax.broadcast_to", _reshape(topi.broadcast_to, "broadcast_to"))
register_legalize("relax.reshape", _reshape(topi.reshape, "reshape"))
register_legalize(
    "relax.collapse_sum_like",
    _reshape(topi.collapse_sum, "collapse_sum", is_collapse_sum_like=True),
)

register_legalize("relax.collapse_sum_to", _reshape(topi.collapse_sum, "collapse_sum"))


@register_legalize("relax.concat")
def _concat(bb: BlockBuilder, call: Call) -> Expr:
    t = call.args[0]
    n_field = len(t.struct_info.fields)
    while isinstance(t, Var):
        binding = bb.lookup_binding(t)
        if not isinstance(binding, (Tuple, Var)):
            break
        t = binding

    assert isinstance(t, (Tuple, Var))
    fields = (
        t.fields if isinstance(t, Tuple) else [bb.emit(TupleGetItem(t, i)) for i in range(n_field)]
    )
    return bb.call_te(
        topi.concatenate, fields, None if call.attrs.axis is None else call.attrs.axis
    )


@register_legalize("relax.expand_dims")
def _expand_dims(bb: BlockBuilder, call: Call) -> Expr:
    def te_expand_dims(data, axis):
        data_relax = relax.Var("data", relax.TensorStructInfo(data.shape))
        f_infer_sinfo = call.op.get_attr("FInferStructInfo")
        output_shape = f_infer_sinfo(relax.op.expand_dims(data_relax, axis), bb).shape
        output_ndim = len(output_shape)

        data_dims = []
        for i in range(output_ndim):
            if i not in axis and (i - output_ndim) not in axis:
                data_dims.append(i)
        return te.compute(
            output_shape,
            lambda *idx: data(*[idx[dim] for dim in data_dims]),
            name="expand_dims",
        )

    return bb.call_te(
        te_expand_dims, call.args[0], call.attrs.axis, primfunc_name_hint="expand_dims"
    )


@register_legalize("relax.flatten")
def _flatten(bb: BlockBuilder, call: Call) -> Expr:
    return bb.call_te(topi.reshape, call.args[0], call.struct_info.shape.values)


@register_legalize("relax.permute_dims")
def _permute_dims(bb: BlockBuilder, call: Call) -> Expr:
    return bb.call_te(topi.transpose, call.args[0], call.attrs.axes)


@register_legalize("relax.split")
def _split(bb: BlockBuilder, call: Call) -> Expr:
    if isinstance(call.attrs.indices_or_sections, tir.IntImm):
        indices_or_sections = call.attrs.indices_or_sections.value
    else:
        indices_or_sections = call.attrs.indices_or_sections
    return bb.call_te(topi.split, call.args[0], indices_or_sections, call.attrs.axis)


@register_legalize("relax.squeeze")
def _squeeze(bb: BlockBuilder, call: Call) -> Expr:
    return bb.call_te(topi.squeeze, call.args[0], call.attrs.axis)


@register_legalize("relax.stack")
def _stack(bb: BlockBuilder, call: Call) -> Expr:
    t = call.args[0]
    n_field = len(t.struct_info.fields)

    # Follow bindings to find the actual tuple
    while isinstance(t, Var):
        binding = bb.lookup_binding(t)
        if not isinstance(binding, (Tuple, Var)):
            break
        t = binding

    assert isinstance(t, (Tuple, Var))

    # Extract fields from either Tuple or bound Var
    fields = (
        t.fields if isinstance(t, Tuple) else [bb.emit(TupleGetItem(t, i)) for i in range(n_field)]
    )

    return bb.call_te(topi.stack, fields, 0 if call.attrs.axis is None else call.attrs.axis.value)


@register_legalize("relax.repeat")
def _repeat(bb: BlockBuilder, call: Call) -> Expr:
    def te_repeat(data: te.Tensor, repeats: IntImm, axis: Optional[IntImm]):
        if axis is None:
            # flatten data
            out_shape = data.shape[0]
            for i in data.shape[1:]:
                out_shape *= i
            data = topi.reshape(data, (out_shape,))
            axis = 0
        # topi only receives int repeats and axis
        return topi.repeat(data, int(repeats), int(axis))

    return bb.call_te(
        te_repeat, call.args[0], call.attrs.repeats, call.attrs.axis, primfunc_name_hint="repeat"
    )


@register_legalize("relax.tile")
def _tile(bb: BlockBuilder, call: Call) -> Expr:
    return bb.call_te(topi.tile, call.args[0], call.attrs.repeats)


@register_legalize("relax.flip")
def _flip(bb: BlockBuilder, call: Call) -> Expr:
    return bb.call_te(topi.flip, call.args[0], int(call.attrs.axis))


@register_legalize("relax.gather_elements")
def _gather_elements(bb: BlockBuilder, call: Call) -> Expr:
    return bb.call_te(topi.gather, call.args[0], int(call.attrs.axis), call.args[1])


@register_legalize("relax.gather_nd")
def _gather_nd(bb: BlockBuilder, call: Call) -> Expr:
    def te_gather_nd(data, indices, batch_dims):
        indices_ndim = len(indices.shape)
        axes = [indices_ndim - 1] + list(range(indices_ndim - 1))
        indices = topi.transpose(indices, axes)
        return topi.gather_nd(data, indices, batch_dims)

    return bb.call_te(te_gather_nd, call.args[0], call.args[1], int(call.attrs.batch_dims))


@register_legalize("relax.index_tensor")
def _index_tensor(bb: BlockBuilder, call: Call) -> Expr:
    t = call.args[1]
    n_field = len(t.struct_info.fields)
    fields = [bb.emit(TupleGetItem(t, i)) for i in range(n_field)]
    return bb.call_te(topi.index_tensor, call.args[0], fields)


@register_legalize("relax.index_put")
def _index_put(bb: BlockBuilder, call: Call) -> Expr:
    data = call.args[0]
    indices = call.args[1]
    values = call.args[2]
    accumulate = call.attrs.accumulate

    # If indices is a Tuple, unpack it into individual tensors
    if isinstance(indices, relax.Tuple):
        indices_list = [indices.fields[i] for i in range(len(indices.fields))]
    else:
        indices_list = [indices]

    return bb.call_te(
        topi.index_put,
        data,
        indices_list,
        values,
        accumulate=accumulate,
    )


@register_legalize("relax.meshgrid")
def _meshgrid(bb: BlockBuilder, call: Call) -> Expr:
    t = call.args[0]
    n_field = len(t.struct_info.fields)
    while isinstance(t, Var):
        binding = bb.lookup_binding(t)
        if not isinstance(binding, (Tuple, Var)):
            break
        t = binding

    assert isinstance(t, (Tuple, Var))
    fields = (
        t.fields if isinstance(t, Tuple) else [bb.emit(TupleGetItem(t, i)) for i in range(n_field)]
    )
    return bb.call_te(
        topi.meshgrid, fields, "ij" if call.attrs.indexing is None else call.attrs.indexing
    )


@register_legalize("relax.scatter_elements")
def _scatter_elements(bb: BlockBuilder, call: Call) -> Expr:
    return bb.call_te(
        topi.scatter_elements,
        call.args[0],
        call.args[1],
        call.args[2],
        call.attrs.axis,
        call.attrs.reduction,
    )


@register_legalize("relax.scatter_nd")
def _scatter_nd(bb: BlockBuilder, call: Call) -> Expr:
    # TODO(relax-team): Support native scatter_nd without te extern
    def scatter_nd(data, indices, updates, reduction):
        axes = list(range(len(indices.shape)))
        indices = topi.transpose(indices, axes[-1:] + axes[:-1])
        return topi.scatter_nd(data, indices, updates, reduction)

    return bb.call_te(
        scatter_nd,
        call.args[0],
        call.args[1],
        call.args[2],
        call.attrs.reduction,
    )


@register_legalize("relax.slice_scatter")
def _slice_scatter(bb: BlockBuilder, call: Call) -> Expr:

    return bb.call_te(
        topi.slice_scatter,
        call.args[0],
        call.args[1],
        call.args[2],
        call.args[3],
        call.args[4],
        call.attrs.axis,
    )


@register_legalize("relax.one_hot")
def _one_hot(bb: BlockBuilder, call: Call) -> Expr:
    indices, on_value, off_value = call.args
    if not (isinstance(on_value, relax.PrimValue) and isinstance(off_value, relax.PrimValue)):
        raise ValueError("on_value and off_value must be PrimValue")
    on_value, off_value = on_value.value, off_value.value
    if on_value.dtype != off_value.dtype:
        raise ValueError("on_value and off_value must have the same dtype")
    return bb.call_te(
        topi.one_hot,
        indices,
        on_value,
        off_value,
        call.attrs.depth,
        call.attrs.axis,
        on_value.dtype,
    )


@register_legalize("relax.layout_transform")
def _layout_transform(bb: BlockBuilder, call: Call) -> Expr:
    def te_layout_transform(data, name):
        """
        Returns a passthrough TE compute with appropriate name. This is needed to generate
        TIR function, output shape info, TIR vars from gen_call_tir_inputs function.
        """
        return te.compute(
            data.shape,
            data,
            name=name,
        )

    def set_axis_sep(axis_sep: list, sch: tir.schedule, buffer_type: str):
        sch.set_axis_separator(primfunc_name, (buffer_type, 0), axis_separators=axis_sep)

    index_map: tvm.tir.IndexMap = call.attrs.index_map
    pad_value = call.attrs.pad_value
    if pad_value is not None:
        pad_value = pad_value.value
    else:
        if "int" in call.args[0].struct_info.dtype:
            pad_value = int(0)
        else:
            pad_value = float(0.0)

    axis_separators: tvm.tir.IndexMap.AXIS_SEPARATOR = call.attrs.axis_separators
    input_axis_separators: tvm.tir.IndexMap.AXIS_SEPARATOR = call.attrs.input_axis_separators

    # Convert to list from array
    axis_separators = [int(sep) for sep in axis_separators]
    primfunc_name = "te_layout_transform"
    _, padding_predicate = index_map.non_surjective_inverse(call.args[0].struct_info.shape)
    if not isinstance(padding_predicate, tvm.tir.expr.IntImm):
        primfunc_name += "_with_pad"
    if len(axis_separators) != 0:
        primfunc_name += "_axis_separator"
    tir_func, call_args, _, tir_vars = gen_call_tir_inputs(
        te_layout_transform, call.args[0], primfunc_name
    )
    # Create TIR schedule to apply layout changes with axis separators
    sch = tir.Schedule(tir_func)
    sch.transform_layout(primfunc_name, ("write", 0), index_map, pad_value)
    set_axis_sep(axis_separators, sch, "write")
    if input_axis_separators is not None:
        set_axis_sep(input_axis_separators, sch, "read")
    gvar = bb.add_func(sch.mod["main"], primfunc_name)
    output_shape = index_map.map_shape(list(call_args[0].struct_info.shape))
    output_dtype = call_args[0].struct_info.dtype
    output_sinfo = [TensorStructInfo(output_shape, output_dtype)]
    return call_tir(gvar, call_args, output_sinfo, tir_vars)
