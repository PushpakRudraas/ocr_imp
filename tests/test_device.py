"""Device selection for the enrichment models: GPU when it really works, loud failure when `cuda` is demanded and is not usable."""
import sys
import types

import pytest

from newspaper_pipeline.device import describe, resolve_device


def fake_torch(monkeypatch, available=True, capability=(12, 0), arches=("sm_90", "sm_120", "compute_120")):
    cuda = types.SimpleNamespace(is_available=lambda: available, get_device_capability=lambda: capability,
                                 get_arch_list=lambda: list(arches), get_device_name=lambda i: "NVIDIA GeForce RTX 5070")
    monkeypatch.setitem(sys.modules, "torch", types.SimpleNamespace(cuda=cuda, __version__="2.9.0+cu128", version=types.SimpleNamespace(cuda="12.8")))


def test_cpu_never_touches_torch(monkeypatch):
    monkeypatch.setitem(sys.modules, "torch", None)             # importing it would fail
    assert resolve_device("cpu") == "cpu"


def test_auto_and_cuda_use_a_usable_gpu(monkeypatch):
    fake_torch(monkeypatch)
    assert resolve_device("auto") == "cuda" and resolve_device("cuda") == "cuda"
    assert "RTX 5070" in describe("cuda") and "CUDA 12.8" in describe("cuda") and describe("cpu") == "cpu"


def test_env_var_is_the_default(monkeypatch):
    fake_torch(monkeypatch)
    monkeypatch.setenv("NP_DEVICE", "cpu")
    assert resolve_device() == "cpu"
    monkeypatch.setenv("NP_DEVICE", "cuda")
    assert resolve_device() == "cuda"


def test_auto_falls_back_to_cpu_but_cuda_refuses_when_there_is_no_gpu(monkeypatch):
    fake_torch(monkeypatch, available=False)
    assert resolve_device("auto") == "cpu"
    with pytest.raises(RuntimeError, match="NP_DEVICE=cuda.*no CUDA GPU is visible"):
        resolve_device("cuda")


def test_a_torch_build_without_kernels_for_the_card_is_not_usable(monkeypatch):
    """The classic RTX 50-series trap: is_available() is True, then the first call dies with 'no kernel image'."""
    fake_torch(monkeypatch, capability=(12, 0), arches=("sm_80", "sm_86", "sm_90"))
    assert resolve_device("auto") == "cpu"
    with pytest.raises(RuntimeError, match="no kernels for the GPU's architecture sm_120"):
        resolve_device("cuda")


def test_missing_torch_and_bad_values(monkeypatch):
    monkeypatch.setitem(sys.modules, "torch", None)
    assert resolve_device("auto") == "cpu"
    with pytest.raises(RuntimeError, match="torch is not installed"):
        resolve_device("cuda")
    with pytest.raises(ValueError):
        resolve_device("tpu")


def test_build_pipeline_runs_the_models_on_the_resolved_device(monkeypatch):
    """The device reaches the embedder and the NLP models, and is reported on the pipeline (and so in the run metrics)."""
    import argparse
    from newspaper_pipeline import cli
    from newspaper_pipeline.cluster import embed
    from newspaper_pipeline.config import Settings
    from newspaper_pipeline.enrich import models

    seen = {}

    class FakeEmbedder(embed.HashingEmbedder):
        def __init__(self, device):
            super().__init__(dim=16)
            seen["embedder"] = device

    class FakeModels:
        def __init__(self, device=None):
            seen["models"] = device
    from newspaper_pipeline import device as dev
    monkeypatch.setattr(dev, "resolve_device", lambda want=None: want or "cpu")       # (resolution itself is tested above)
    monkeypatch.setattr(dev, "describe", lambda d: d)
    monkeypatch.setattr(embed, "minilm_embedder", lambda device=None: FakeEmbedder(device))
    monkeypatch.setattr(models, "HfModels", FakeModels)
    args = argparse.Namespace(ocr="none", ocr_lang="eng", translator="none", embedder="minilm", store="sqlite", index="memory", graph="none",
                              qdrant_path=None, judge="none", llm_refine=False, models="hf", device="cuda")
    p = cli.build_pipeline(args, Settings(db_path=":memory:", embed_dim=16), {}, "enrich")
    assert seen == {"embedder": "cuda", "models": "cuda"} and p.device == "cuda"
    cpu = cli.build_pipeline(argparse.Namespace(**{**vars(args), "device": "cpu"}), Settings(db_path=":memory:", embed_dim=16), {}, "enrich")
    assert seen == {"embedder": "cpu", "models": "cpu"} and cpu.device == "cpu"
