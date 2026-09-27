# Nothing to install — the point of a stdlib-only server is that its image is
# the base image plus three files, one of which is the console.
FROM python:3.12-slim

WORKDIR /app
COPY server.py responses.json ui.html ./

ENV LLMSIM_PORT=8099
EXPOSE 8099

# Unbuffered, so the per-request log line appears as it happens rather than
# whenever the buffer decides. A simulator you cannot watch is hard to trust.
ENTRYPOINT ["python3", "-u", "server.py"]
