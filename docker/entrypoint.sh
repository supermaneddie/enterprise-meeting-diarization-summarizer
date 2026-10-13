#!/bin/sh
set -eu

# Generate unique host keys on first container startup, never in the image.
mkdir -p /run/sshd

# Bind mounts retain host ownership. Stage a private root-owned copy for sshd
# without changing the mounted file (which may be read-only).
if [ -e /root/.ssh/authorized_keys ]; then
    if [ ! -f /root/.ssh/authorized_keys ]; then
        echo "SSH authorized_keys mount must be a file" >&2
        exit 1
    fi
    install -o root -g root -m 0600 /root/.ssh/authorized_keys /run/sshd/root-authorized_keys
else
    # Avoid retaining keys from a previous startup when no source is present.
    rm -f /run/sshd/root-authorized_keys
fi

ssh-keygen -A
/usr/sbin/sshd -f /etc/ssh/sshd_config.transcription

# Preserve NVIDIA's startup wrapper when provided by the base image.
if [ -x /opt/nvidia/nvidia_entrypoint.sh ]; then
    exec /opt/nvidia/nvidia_entrypoint.sh "$@"
fi
exec "$@"
