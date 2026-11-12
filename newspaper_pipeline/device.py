"""Which device the enrichment models (sentence-transformer embedder + NER / topic / sentiment / emotion) run on.

    NP_DEVICE=auto   (default)  GPU if torch can use one, otherwise CPU
    NP_DEVICE=cuda              GPU or fail: never silently fall back to the CPU
    NP_DEVICE=cpu               CPU

"Can use" means more than `torch.cuda.is_available()`: the installed torch build must also contain kernels for the card's
architecture (a new GPU such as the RTX 50-series needs a CUDA 12.8+ build; an older build reports the GPU as available
and then dies with "no kernel image is available" on the first model call)."""
from __future__ import annotations

import logging
import os

log = logging.getLogger("np.device")


def _usable_cuda() -> tuple[bool, str]:
    """(usable, reason-if-not)."""
    try:
        import torch
    except Exception as e:
        return False, f"torch is not installed ({type(e).__name__})"
    if not torch.cuda.is_available():
        return False, ("no CUDA GPU is visible to torch (is the container started with GPU access - "
                       "compose `deploy.resources.reservations.devices` / `--gpus all` - and is torch a CUDA build?)")
    major, minor = torch.cuda.get_device_capability()
    arches = torch.cuda.get_arch_list()
    if f"sm_{major}{minor}" not in arches and f"compute_{major}{minor}" not in arches:
        return False, (f"this torch build ({torch.__version__}) has no kernels for the GPU's architecture sm_{major}{minor}; "
                       f"it supports {arches}. Rebuild the image with a newer CUDA wheel (TORCH_INDEX build arg)")
    return True, ""


def resolve_device(want: str | None = None) -> str:
    """"cuda" or "cpu". Raises if `cuda` was asked for explicitly but cannot be used."""
    want = (want or os.environ.get("NP_DEVICE", "auto")).strip().lower()
    if want not in ("auto", "cuda", "cpu"):
        raise ValueError(f"NP_DEVICE must be auto, cuda or cpu, not {want!r}")
    if want == "cpu":
        return "cpu"
    ok, why = _usable_cuda()
    if ok:
        return "cuda"
    if want == "cuda":
        raise RuntimeError(f"NP_DEVICE=cuda but the GPU cannot be used: {why}")
    log.info("running on the CPU: %s", why)
    return "cpu"


def describe(device: str) -> str:
    if device == "cuda":
        import torch
        return f"cuda ({torch.cuda.get_device_name(0)}, torch {torch.__version__}, CUDA {torch.version.cuda})"
    return "cpu"
