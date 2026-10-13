FROM qwenllm/qwen3-asr:latest

USER root

# TorchCodec needs FFmpeg's shared libraries as well as the ffmpeg executable.
RUN apt-get update && apt-get install -y --no-install-recommends openssh-server ffmpeg \
    && rm -rf /var/lib/apt/lists/* \
    && rm -f /etc/ssh/ssh_host_* \
    && install -d -m 0700 /root/.ssh

WORKDIR /app

COPY requirements.txt requirements-asr.txt requirements-diarization.txt ./
RUN pip3 install --no-cache-dir \
    -r requirements-asr.txt \
    -r requirements-diarization.txt

# Match TorchCodec to the resolved Torch ABI. Audio decoding only needs the CPU
# wheel; a default PyPI CUDA wheel can require a different CUDA runtime from ASR.
# --no-deps preserves the CUDA-enabled Torch stack used for model inference.
# Compatibility: https://github.com/meta-pytorch/torchcodec#compatibility-with-torch-versions
RUN python3 - <<'PY'
import subprocess
import sys
from importlib.metadata import version

torch_minor = tuple(map(int, version("torch").split(".")[:2]))
codec_versions = {(2, 8): "0.7", (2, 9): "0.9", (2, 10): "0.10"}
if torch_minor >= (2, 11):
    codec_version = "0.12"  # Stable ABI: supports Torch >= 2.11.
else:
    codec_version = codec_versions[torch_minor]
subprocess.run([
    sys.executable, "-m", "pip", "install", "--no-cache-dir",
    "--force-reinstall", "--no-deps",
    "--index-url", "https://download.pytorch.org/whl/cpu",
    f"torchcodec=={codec_version}.*",
], check=True)
PY

# Fail the image build if native libraries cannot load or audio cannot decode.
# This check does not need a GPU or downloaded model weights.
RUN python3 - <<'PY'
import tempfile
import wave
from torchcodec.decoders import AudioDecoder

with tempfile.NamedTemporaryFile(suffix=".wav") as source:
    with wave.open(source.name, "wb") as audio:
        audio.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        audio.writeframes(b"\x00\x00" * 16000)
    decoded = AudioDecoder(source.name).get_all_samples()
    assert decoded.sample_rate == 16000
    assert tuple(decoded.data.shape) == (1, 16000)
PY

COPY . .
COPY docker/sshd_config /etc/ssh/sshd_config.transcription
RUN chmod 0755 /app/docker/entrypoint.sh
ENV DATA_DIR=/data
VOLUME ["/data"]
EXPOSE 8090 22
ENTRYPOINT ["/app/docker/entrypoint.sh"]
CMD ["python3", "run.py"]
