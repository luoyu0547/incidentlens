# Disposable SSH target for the opt-in live integration test
# (tests/integration/test_live_ssh_tools.py).
#
# This image intentionally installs nothing beyond an OpenSSH server.  It
# creates a non-root user and a /workspace/service directory so the Phase 2
# remote tools (persistent shell, scoped file tools, change manager) have a
# real target to exercise.  It is NOT a production service image.
FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends openssh-server \
    && rm -rf /var/lib/apt/lists/*

# Create a non-root user and the service workspace the tests operate on.
RUN useradd -m -s /bin/sh incidentlens \
    && mkdir -p /workspace/service \
    && chown -R incidentlens:incidentlens /workspace \
    && mkdir -p /home/incidentlens/.ssh \
    && chown incidentlens:incidentlens /home/incidentlens/.ssh \
    && chmod 700 /home/incidentlens/.ssh

# Public-key-only authentication.  The compose file bind-mounts the test
# generated public key read-only at /home/incidentlens/.ssh/authorized_keys.
RUN sed -i 's/^#\?PasswordAuthentication.*/PasswordAuthentication no/' /etc/ssh/sshd_config \
    && echo 'PubkeyAuthentication yes' >> /etc/ssh/sshd_config \
    && echo 'PermitRootLogin no' >> /etc/ssh/sshd_config \
    && echo 'AllowUsers incidentlens' >> /etc/ssh/sshd_config

EXPOSE 22

# Generate ephemeral host keys on every start, then run sshd in the foreground
# so the container stays alive and logs go to stdout/stderr.
CMD ["sh", "-c", "ssh-keygen -A && exec /usr/sbin/sshd -D -e"]
