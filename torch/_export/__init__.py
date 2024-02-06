import copy
import dataclasses
import functools
import io
import json
import os
import re
import sys
import types
import warnings
import weakref
import zipfile
from collections import OrderedDict
from contextlib import contextmanager

from typing import Any, Callable, Dict, List, Optional, Tuple, Union
from unittest.mock import patch

import sympy

import torch
import torch._dynamo
import torch.fx
import torch.utils._pytree as pytree

from torch._decomp import core_aten_decompositions, get_decompositions
from torch._dispatch.python import enable_python_dispatcher
from torch._dynamo.exc import UserError, UserErrorType
from torch._dynamo.source import ConstantSource
from torch._export.passes.collect_tracepoints_pass import CollectTracepointsPass
from torch._functorch.aot_autograd import aot_export_module, GraphSignature
from torch._functorch.eager_transforms import functionalize
from torch._guards import detect_fake_mode
from torch._ops import OpOverload
from torch._subclasses.fake_tensor import FakeTensor, FakeTensorMode
from torch._subclasses.functional_tensor import FunctionalTensor
from torch.export._tree_utils import reorder_kwargs
from torch.export.exported_program import (
    ExportedProgram,
    ModuleCallEntry,
    ModuleCallSignature,
    _disable_prexisiting_fake_mode,
)
from torch.export.graph_signature import (
    _sig_to_specs,
    ArgumentSpec,
    ConstantArgument,
    ExportGraphSignature,
    InputKind,
    InputSpec,
    OutputKind,
    OutputSpec,
    SymIntArgument,
    TensorArgument,
)
from torch.export.dynamic_shapes import (
    Constraint,
    dims,
    dynamic_dim,
    _process_constraints,
    _process_dynamic_shapes,
)
from torch.export._unlift import _create_stateful_graph_module
from torch.fx import traceback as fx_traceback
from torch.fx._compatibility import compatibility
from torch.fx.experimental.proxy_tensor import make_fx, maybe_disable_fake_tensor_mode
from torch.fx.experimental.symbolic_shapes import (
    ConstraintViolationError,
    GuardOnDataDependentSymNode,
    ShapeEnv,
    StrictMinMaxConstraint,
)
from torch.fx.graph import _PyTreeCodeGen, _PyTreeInfo
from torch.utils._sympy.value_ranges import ValueRangeError, ValueRanges

from .exported_program import (
    CallSpec,
)
from .passes.add_runtime_assertions_for_constraints_pass import (
    _AddRuntimeAssertionsForInlineConstraintsPass,
)
from .wrappers import _wrap_submodules
from torch._inductor import config


@dataclasses.dataclass
class ExportDynamoConfig:
    """
    Manage Export-specific configurations of Dynamo.
    """
    allow_rnn: bool = True


@compatibility(is_backward_compatible=False)
def capture_pre_autograd_graph(
    f: Callable,
    args: Tuple[Any],
    kwargs: Optional[Dict[str, Any]] = None,
    constraints: Optional[List[Constraint]] = None,
    dynamic_shapes: Optional[Union[Dict[str, Any], Tuple[Any]]] = None,
) -> torch.nn.Module:
    """
    A helper function that is intended to trace a module before any pre-autograd
    decomposition is run. The produced module will be "non-functional" and
    composed of aten operators. Later this API will be deleted in favor of more general
    torch.export API.

    Args:
      f: A callable to be traced

      args: example positional inputs.

      kwargs: optional example keyword inputs.

      constraints: [DEPRECATED: use ``dynamic_shapes`` instead, see below]
         An optional list of constraints on the dynamic arguments
         that specify their possible range of shapes. By default, shapes of
         input torch.Tensors are assumed to be static. If an input torch.Tensor
         is expected to have dynamic shapes, please use :func:`dynamic_dim`
         to define :class:`Constraint` objects that specify the dynamics and the possible
         range of shapes. See :func:`dynamic_dim` docstring for examples on
         how to use it.

      dynamic_shapes: Should either be:
         1) a dict from argument names of ``f`` to their dynamic shape specifications,
         2) a tuple that specifies dynamic shape specifications for each input in original order.
         If you are specifying dynamism on keyword args, you will need to pass them in the order that
         is defined in the original function signature.

         The dynamic shape of a tensor argument can be specified as either
         (1) a dict from dynamic dimension indices to :func:`Dim` types, where it is
         not required to include static dimension indices in this dict, but when they are,
         they should be mapped to None; or (2) a tuple / list of :func:`Dim` types or None,
         where the :func:`Dim` types correspond to dynamic dimensions, and static dimensions
         are denoted by None. Arguments that are dicts or tuples / lists of tensors are
         recursively specified by using mappings or sequences of contained specifications.

    Returns:
        An nn.Module containing the traced method.

    """
    from torch.export._trace import _convert_input_to_fake, DEFAULT_EXPORT_DYNAMO_CONFIG
    from torch.export.dynamic_shapes import _process_dynamic_shapes

    if kwargs is None:
        kwargs = {}

    if constraints is not None:
        warnings.warn(
            "Using `constraints` to specify dynamic shapes for export is DEPRECATED "
            "and will not be supported in the future. "
            "Please use `dynamic_shapes` instead (see docs on `torch.export.export`).",
            DeprecationWarning,
            stacklevel=2,
        )
    else:
        constraints = _process_dynamic_shapes(f, args, kwargs, dynamic_shapes)

    # Do not decompose dropout for exported models, because in eval mode the dropout
    # op disappears from the graph, which makes it difficult to switch to train mode.
    # See https://github.com/pytorch/pytorch/pull/115258#issuecomment-1900755832.
    decomp_table = {
        op: op.decompose
        for op in FunctionalTensor.maybe_aliasing_or_mutating_ops
        if op != torch.ops.aten.dropout.default
    }
    with torch._dynamo.config.patch(dataclasses.asdict(DEFAULT_EXPORT_DYNAMO_CONFIG)):
        m = torch._dynamo.export(
            f,
            constraints=constraints,
            assume_static_by_default=True,
            tracing_mode="symbolic",
            decomposition_table=decomp_table,
            pre_dispatch=True,
            aten_graph=True,
        )(
            *args,
            **kwargs,
        )[0]

        _, _, _, fake_mode = _convert_input_to_fake(m, args, kwargs)

        m.meta["inline_constraints"] = {
            k: v
            for k, v in fake_mode.shape_env.runtime_var_to_range.items()
            if re.match(r"^[if]\d+$", str(k))
        }

        if isinstance(f, torch.nn.Module):
            from torch.export._trace import _restore_state_dict
            _restore_state_dict(f, m)

        flat_args, _ = pytree.tree_flatten((args, kwargs or {}))
        range_constraints = _process_constraints(m, 0, flat_args)
        module = _create_stateful_graph_module(
            m,
            range_constraints=range_constraints,
        )

    def _train(self, mode: bool = True):
        raise NotImplementedError("Calling train() is not supported yet.")

    def _eval(self, mode: bool = True):
        raise NotImplementedError("Calling eval() is not supported yet.")

    module.train = types.MethodType(_train, module)  # type: ignore[method-assign]
    module.eval = types.MethodType(_eval, module)  # type: ignore[method-assign]
    return module


def save(
    ep: ExportedProgram,
    f: Union[str, os.PathLike, io.BytesIO],
    *,
    extra_files: Optional[Dict[str, Any]] = None,
    opset_version: Optional[Dict[str, int]] = None,
) -> None:
    if not isinstance(ep, ExportedProgram):
        raise TypeError(f"save() expects an ExportedProgram but got {type(ep)}")

    from .serde.serialize import serialize, SerializedArtifact
    from .serde.schema import SCHEMA_VERSION
    artifact: SerializedArtifact = serialize(ep, opset_version)

    if isinstance(f, (str, os.PathLike)):
        f = os.fspath(f)

    with zipfile.ZipFile(f, 'w') as zipf:
        # Save every field the SerializedArtifact to a file
        assert isinstance(artifact.exported_program, bytes)
        zipf.writestr("serialized_exported_program.json", artifact.exported_program)
        zipf.writestr("serialized_state_dict.pt", artifact.state_dict)
        zipf.writestr("serialized_constants.pt", artifact.constants)

        zipf.writestr('version', ".".join(map(str, SCHEMA_VERSION)))

        # Add extra files if provided
        if extra_files:
            for extra_file_name, content in extra_files.items():
                encoded_content = content.encode('utf-8')
                zipf.writestr(f"extra_files/{extra_file_name}", encoded_content)


def load(
    f: Union[str, os.PathLike, io.BytesIO],
    *,
    extra_files: Optional[Dict[str, Any]] = None,
    expected_opset_version: Optional[Dict[str, int]] = None,
) -> ExportedProgram:
    if isinstance(f, (str, os.PathLike)):
        f = os.fspath(f)

    extra_files = extra_files or {}

    with zipfile.ZipFile(f, 'r') as zipf:
        # Check the version
        version = zipf.read('version').decode().split('.')
        from .serde.schema import SCHEMA_VERSION

        assert len(version) == len(SCHEMA_VERSION)
        if version[0] != str(SCHEMA_VERSION[0]):
            raise RuntimeError(
                f"Serialized version {version} does not match our current "
                f"schema version {SCHEMA_VERSION}."
            )

        from .serde.serialize import deserialize, SerializedArtifact

        # Load serialized_ep and serialized_state_dict from the zip file

        serialized_exported_program: Optional[bytes] = None
        serialized_state_dict: Optional[bytes] = None
        serialized_constants: Optional[bytes] = None

        for file_info in zipf.infolist():
            file_content = zipf.read(file_info.filename)

            if file_info.filename == "serialized_exported_program.json":
                serialized_exported_program = file_content
            elif file_info.filename == "serialized_state_dict.json":
                warnings.warn("This version of file is deprecated")
                serialized_state_dict = file_content
            elif file_info.filename == "serialized_constants.json":
                warnings.warn("This version of file is deprecated")
                serialized_constants = file_content
            elif file_info.filename == "serialized_state_dict.pt":
                serialized_state_dict = file_content
            elif file_info.filename == "serialized_constants.pt":
                serialized_constants = file_content
            elif file_info.filename.startswith("extra_files"):
                filename = file_info.filename.split("/", 1)[1]
                extra_files[filename] = file_content.decode('utf-8')

        assert serialized_exported_program is not None
        assert serialized_state_dict is not None
        assert serialized_constants is not None
        artifact: SerializedArtifact = SerializedArtifact(
            serialized_exported_program,
            serialized_state_dict,
            serialized_constants,
        )

        # Deserialize ExportedProgram
        ep = deserialize(artifact, expected_opset_version)

        return ep


def aot_compile(
    f: Callable,
    args: Tuple[Any],
    kwargs: Optional[Dict[str, Any]] = None,
    *,
    constraints: Optional[List[Constraint]] = None,
    dynamic_shapes: Optional[Dict[str, Any]] = None,
    options: Optional[Dict[str, Any]] = None,
    remove_runtime_assertions: bool = False,
    disable_constraint_solver: bool = False,
) -> str:
    """
    Note: this function is not stable yet

    Traces either an nn.Module's forward function or just a callable with PyTorch
    operations inside, generates executable cpp code from the program, and returns
    the path to the generated shared library

    Args:
        f: the `nn.Module` or callable to trace.

        args: example positional inputs.

        kwargs: optional example keyword inputs.

        constraints: A optional list of constraints on the dynamic arguments specifying
            their possible range of their shapes

        dynamic_shapes: An experimental new feature designed to subsume ``constraints``.
            A dict mapping argument names of ``f`` to their dynamic shape
            specifications, as follows. Dynamic shape specifications can be a
            dict from dynamic dimensions to ``Dim`` types, or a tuple/list of
            ``Optional[Dim]`` corresponding to each input dimension.

        options: A dictionary of options to control inductor

        disable_constraint_solver: Whether the dim constraint solver must be disabled.

    Returns:
        Path to the generated shared library
    """
    if constraints is not None:
        warnings.warn(
            "The constraints field is deprecated. "
            "Please use dynamic_shapes instead."
        )

    from torch.export._trace import _export_to_torch_ir
    from torch._inductor.decomposition import select_decomp_table

    if constraints is None:
        constraints = _process_dynamic_shapes(f, args, kwargs, dynamic_shapes)

    if config.is_predispatch:
        gm = torch.export._trace._export(f, args, kwargs, constraints, pre_dispatch=True).module()
    else:
        # We want to export to Torch IR here to utilize the pre_grad passes in
        # inductor, which run on Torch IR.
        gm = _export_to_torch_ir(
            f,
            args,
            kwargs,
            constraints,
            disable_constraint_solver=disable_constraint_solver,
            # Disabling this flag, because instead we can rely on the mapping
            # dynamo_flat_name_to_original_fqn which is coming from Dynamo.
            restore_fqn=False,
        )
    flat_example_inputs = pytree.arg_tree_leaves(*args, **(kwargs or {}))

    with torch.no_grad():
        so_path = torch._inductor.aot_compile(gm, flat_example_inputs, options)  # type: ignore[arg-type]

    return so_path

def aot_load(so_path: str, device: str) -> Callable:
    """
    Loads a shared library generated by aot_compile and returns a callable

    Args:
        so_path: Path to the shared library

    Returns:
        A callable
    """
    if device == "cpu":
        runner = torch._C._aoti.AOTIModelContainerRunnerCpu(so_path, 1)  # type: ignore[call-arg]
    elif device == "cuda" or device.startswith("cuda:"):
        runner = torch._C._aoti.AOTIModelContainerRunnerCuda(so_path, 1, device)  # type: ignore[assignment, call-arg]
    else:
        raise RuntimeError("Unsupported device " + device)

    def optimized(*args, **kwargs):
        call_spec = runner.get_call_spec()  # type: ignore[attr-defined]
        in_spec = pytree.treespec_loads(call_spec[0])
        out_spec = pytree.treespec_loads(call_spec[1])
        flat_inputs = pytree.tree_flatten((args, reorder_kwargs(kwargs, in_spec)))[0]
        flat_outputs = runner.run(flat_inputs)  # type: ignore[attr-defined]
        return pytree.tree_unflatten(flat_outputs, out_spec)

    return optimized
