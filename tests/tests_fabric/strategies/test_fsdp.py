# Copyright The Lightning AI team.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
from datetime import timedelta
from re import escape
from unittest import mock
from unittest.mock import ANY, MagicMock, Mock

import pytest
import torch
import torch.nn as nn
from torch.optim import Adam

from lightning.fabric.plugins.environments import LightningEnvironment
from lightning.fabric.strategies import FSDPStrategy
from lightning.fabric.strategies.fsdp import _FSDPBackwardSyncControl
from lightning.fabric.utilities.imports import _TORCH_GREATER_EQUAL_1_12
from tests_fabric.helpers.runif import RunIf
from tests_fabric.strategies.test_single_device import _MyFabricGradNorm

if _TORCH_GREATER_EQUAL_1_12:
    from torch.distributed.fsdp.fully_sharded_data_parallel import CPUOffload, FullyShardedDataParallel, MixedPrecision


@mock.patch("lightning.fabric.strategies.fsdp._TORCH_GREATER_EQUAL_1_12", False)
def test_fsdp_support(*_):
    with pytest.raises(NotImplementedError, match="`FSDPStrategy` is supported from PyTorch v1.12.0"):
        FSDPStrategy()


@RunIf(min_torch="1.12")
def test_fsdp_custom_mixed_precision():
    """Test that passing a custom mixed precision config works."""
    config = MixedPrecision()
    strategy = FSDPStrategy(mixed_precision=config)
    assert strategy.mixed_precision_config == config


@RunIf(min_torch="1.12")
def test_fsdp_cpu_offload():
    """Test the different ways cpu offloading can be enabled."""
    # bool
    strategy = FSDPStrategy(cpu_offload=True)
    assert strategy.cpu_offload == CPUOffload(offload_params=True)

    # dataclass
    config = CPUOffload()
    strategy = FSDPStrategy(cpu_offload=config)
    assert strategy.cpu_offload == config


@RunIf(min_torch="1.12")
@pytest.mark.parametrize("torch_ge_2_0", [False, True])
def test_fsdp_setup_optimizer_validation(torch_ge_2_0):
    """Test that `setup_optimizer()` validates the param groups and reference to FSDP parameters."""
    module = nn.Linear(2, 2)
    strategy = FSDPStrategy(parallel_devices=[torch.device("cpu")])

    with mock.patch("lightning.fabric.strategies.fsdp._TORCH_GREATER_EQUAL_2_0", torch_ge_2_0):
        bad_optimizer_1 = Adam([{"params": [module.weight]}, {"params": [module.bias], "lr": 1e-3}])
        bad_optimizer_2 = Adam(module.parameters())

        if torch_ge_2_0:
            strategy.setup_optimizer(bad_optimizer_1)
            strategy.setup_optimizer(bad_optimizer_2)
        else:
            with pytest.raises(ValueError, match="does not support multiple param groups"):
                strategy.setup_optimizer(bad_optimizer_1)
            with pytest.raises(ValueError, match="The optimizer does not seem to reference any FSDP parameter"):
                strategy.setup_optimizer(bad_optimizer_2)


@RunIf(min_torch="2.0.0")
@mock.patch("lightning.fabric.strategies.fsdp.FSDPStrategy.setup_module")
def test_fsdp_setup_use_orig_params(_):
    module = nn.Linear(2, 2)
    optimizer = Adam(module.parameters())

    strategy = FSDPStrategy(parallel_devices=[torch.device("cpu")], use_orig_params=False)
    assert not strategy._fsdp_kwargs["use_orig_params"]

    with pytest.raises(ValueError, match=r"`FSDPStrategy\(use_orig_params=False\)` but this is not supported"):
        strategy.setup_module_and_optimizers(module, optimizer)

    strategy = FSDPStrategy(parallel_devices=[torch.device("cpu")])
    assert strategy._fsdp_kwargs["use_orig_params"]
    strategy.setup_module_and_optimizers(module, optimizer)
    assert strategy._fsdp_kwargs["use_orig_params"]


@RunIf(min_torch="1.12")
def test_fsdp_no_backward_sync():
    """Test that the backward sync control calls `.no_sync()`, and only on a module wrapped in
    FullyShardedDataParallel."""

    strategy = FSDPStrategy()
    assert isinstance(strategy._backward_sync_control, _FSDPBackwardSyncControl)

    with pytest.raises(
        TypeError, match="is only possible if the module passed to .* is wrapped in `FullyShardedDataParallel`"
    ), strategy._backward_sync_control.no_backward_sync(Mock()):
        pass

    module = MagicMock(spec=FullyShardedDataParallel)
    with strategy._backward_sync_control.no_backward_sync(module):
        pass

    module.no_sync.assert_called_once()


@RunIf(min_torch="1.12")
@mock.patch("lightning.fabric.strategies.fsdp._TORCH_GREATER_EQUAL_1_13", False)
def test_fsdp_activation_checkpointing_support():
    """Test that we error out if activation checkpointing requires a newer PyTorch version."""
    with pytest.raises(ValueError, match="Activation checkpointing requires torch >= 1.13.0"):
        FSDPStrategy(activation_checkpointing=Mock())


@RunIf(min_torch="1.13")
def test_fsdp_activation_checkpointing():
    """Test that the FSDP strategy can apply activation checkpointing to the given layers."""

    class Block1(nn.Linear):
        pass

    class Block2(nn.Linear):
        pass

    class Model(nn.Module):
        def __init__(self):
            super().__init__()
            self.layer0 = nn.Sequential(Block1(4, 4), Block1(5, 5))
            self.layer1 = Block2(2, 2)
            self.layer2 = nn.Linear(3, 3)

    strategy = FSDPStrategy(activation_checkpointing=Block1)
    assert strategy._activation_checkpointing == [Block1]

    strategy = FSDPStrategy(activation_checkpointing=[Block1, Block2])
    assert strategy._activation_checkpointing == [Block1, Block2]

    strategy._parallel_devices = [torch.device("cuda", 0)]
    with mock.patch(
        "torch.distributed.fsdp.fully_sharded_data_parallel.FullyShardedDataParallel"
    ) as fsdp_mock, mock.patch(
        "torch.distributed.algorithms._checkpoint.checkpoint_wrapper.apply_activation_checkpointing"
    ) as ckpt_mock:
        strategy.setup_module(Model())
        ckpt_mock.assert_called_with(fsdp_mock(), checkpoint_wrapper_fn=ANY, check_fn=ANY)


@RunIf(min_torch="1.13")
def test_fsdp_grad_clipping_value_error():
    strategy = FSDPStrategy()
    with pytest.raises(
        NotImplementedError,
        match=(
            "FSDP currently does not support to clip gradients by value. "
            "Consider clipping by norm instead or choose another strategy!"
        ),
    ):
        strategy.clip_gradients_value(Mock(), Mock(), Mock())


class _MyFSDPFabricGradientNorm(_MyFabricGradNorm):
    def after_backward(self, model, optimizer):
        self.clip_gradients(model, optimizer, max_norm=0.05, error_if_nonfinite=True)

        with model._forward_module.summon_full_params(model._forward_module):
            parameters = model.parameters()
            grad_norm = torch.linalg.vector_norm(
                torch.stack([torch.linalg.vector_norm(p.grad.detach(), 2, dtype=torch.float32) for p in parameters]),
                2,
            )
            torch.testing.assert_close(grad_norm, torch.tensor(0.05, device=self.device))


@pytest.mark.parametrize(
    "precision",
    ["32-true", "16-mixed", pytest.param("bf16-mixed", marks=RunIf(bf16_cuda=True))],
)
@RunIf(min_cuda_gpus=2, standalone=True)
@pytest.mark.xfail(reason="Testing with FSDP is not yet correct")  # TODO: Investigate testing with fsdp
def test_fsdp_grad_clipping_norm(precision):
    fabric = _MyFSDPFabricGradientNorm(accelerator="cuda", devices=2, precision=precision, strategy="fsdp")
    fabric.run()


@RunIf(min_torch="2.0.0")
def test_fsdp_save_checkpoint_storage_options(tmp_path):
    """Test that the FSDP strategy does not accept storage options for saving checkpoints."""
    strategy = FSDPStrategy()
    with pytest.raises(TypeError, match=escape("FSDPStrategy.save_checkpoint(..., storage_options=...)` is not")):
        strategy.save_checkpoint(path=tmp_path, state=Mock(), storage_options=Mock())


@RunIf(min_torch="2.0.0")
@mock.patch("lightning.fabric.strategies.fsdp.FSDPStrategy.broadcast", lambda _, x: x)
def test_fsdp_save_checkpoint_folder_exists(tmp_path):
    path = tmp_path / "exists"
    path.mkdir()
    (path / "file").touch()
    strategy = FSDPStrategy()
    with pytest.raises(FileExistsError, match="exists and is not empty"):
        strategy.save_checkpoint(path=path, state=Mock())


@RunIf(min_torch="2.0.0")
@mock.patch("lightning.fabric.strategies.fsdp.FSDPStrategy.broadcast", lambda _, x: x)
def test_fsdp_save_checkpoint_one_fsdp_module_required(tmp_path):
    """Test that the FSDP strategy can only save one FSDP model per checkpoint."""
    strategy = FSDPStrategy()

    # missing FSDP model
    with pytest.raises(ValueError, match="Could not find a FSDP model in the provided checkpoint state."):
        strategy.save_checkpoint(path=tmp_path, state={})
    with pytest.raises(ValueError, match="Could not find a FSDP model in the provided checkpoint state."):
        strategy.save_checkpoint(path=tmp_path, state={"model": torch.nn.Linear(3, 3)})

    # multiple FSDP models
    model1 = Mock(spec=FullyShardedDataParallel)
    model2 = Mock(spec=FullyShardedDataParallel)
    with pytest.raises(ValueError, match="Found multiple FSDP modules in the given state."):
        strategy.save_checkpoint(path=tmp_path, state={"model1": model1, "model2": model2})


@RunIf(min_torch="2.0.0")
def test_fsdp_load_checkpoint_no_state(tmp_path):
    """Test that the FSDP strategy can't load the full state without access to a model instance from the user."""
    strategy = FSDPStrategy()
    with pytest.raises(ValueError, match=escape("Got FSDPStrategy.load_checkpoint(..., state=None")):
        strategy.load_checkpoint(path=tmp_path, state=None)
    with pytest.raises(ValueError, match=escape("Got FSDPStrategy.load_checkpoint(..., state={})")):
        strategy.load_checkpoint(path=tmp_path, state={})


@RunIf(min_torch="2.0.0")
@mock.patch("lightning.fabric.strategies.fsdp.FSDPStrategy.broadcast", lambda _, x: x)
def test_fsdp_load_checkpoint_one_fsdp_module_required(tmp_path):
    """Test that the FSDP strategy can only load one FSDP model per checkpoint."""
    strategy = FSDPStrategy()

    # missing FSDP model
    with pytest.raises(ValueError, match="Could not find a FSDP model in the provided checkpoint state."):
        strategy.load_checkpoint(path=tmp_path, state={"other": "data"})
    with pytest.raises(ValueError, match="Could not find a FSDP model in the provided checkpoint state."):
        strategy.load_checkpoint(path=tmp_path, state={"model": torch.nn.Linear(3, 3)})

    # multiple FSDP models
    model1 = Mock(spec=FullyShardedDataParallel)
    model2 = Mock(spec=FullyShardedDataParallel)
    with pytest.raises(ValueError, match="Found multiple FSDP modules in the given state."):
        strategy.load_checkpoint(path=tmp_path, state={"model1": model1, "model2": model2})


@RunIf(min_torch="1.12")
@mock.patch("torch.distributed.init_process_group")
def test_set_timeout(init_process_group_mock):
    """Test that the timeout gets passed to the ``torch.distributed.init_process_group`` function."""
    test_timedelta = timedelta(seconds=30)
    strategy = FSDPStrategy(timeout=test_timedelta, parallel_devices=[torch.device("cpu")])
    strategy.cluster_environment = LightningEnvironment()
    strategy.accelerator = Mock()
    strategy.setup_environment()
    process_group_backend = strategy._get_process_group_backend()
    global_rank = strategy.cluster_environment.global_rank()
    world_size = strategy.cluster_environment.world_size()
    init_process_group_mock.assert_called_with(
        process_group_backend, rank=global_rank, world_size=world_size, timeout=test_timedelta
    )
