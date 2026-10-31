# Nothing to install — the point of a stdlib-only server is that its image is
# the base image plus four files: the server, its script, the console and the guide.
FROM python:3.12-slim

WORKDIR /app
COPY server.py responses.json ui.html ./
COPY testkit ./testkit
COPY docs/guide.html ./docs/guide.html

ENV LLMSIM_PORT=8099
# What LLMSim writes for itself (keys, routing, your
# rules, runs) goes here, and only here. Mount a volume on it and a new
# container picks up where the old one stopped; without one, Docker still gives
# it an anonymous volume, so a restart of the same container keeps it too.
ENV LLMSIM_DATA=/data
RUN mkdir -p /data && chmod 700 /data
VOLUME /data
EXPOSE 8099

# Unbuffered, so the per-request log line appears as it happens rather than
# whenever the buffer decides. A simulator you cannot watch is hard to trust.
ENTRYPOINT ["python3", "-u", "server.py"]
