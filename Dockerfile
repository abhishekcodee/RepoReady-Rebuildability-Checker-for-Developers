FROM python:3.12.7-slim

COPY --from=ghcr.io/astral-sh/uv:0.13.1 /uv /uvx /bin/

WORKDIR /workspace

COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src ./src

RUN uv sync --locked \
    && groupadd --system repoready \
    && useradd --system --gid repoready --create-home --home-dir /home/repoready repoready \
    && mkdir -p /target /report \
    && chown -R repoready:repoready /workspace /target /report

ENV PATH="/workspace/.venv/bin:${PATH}"

VOLUME ["/target", "/report"]

USER repoready

ENTRYPOINT ["repoready"]
CMD ["/target", "--json", "/report/report.json"]
