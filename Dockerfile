# syntax=docker/dockerfile:1
# Network only in the deps stage, which sees nothing but the lock files and
# downloads wheels without running any package code (SPEC §11). Everything
# after it installs, tests and runs with --network=none.

FROM python:3.12-slim AS deps
WORKDIR /deps
COPY requirements.txt requirements-dev.txt ./
RUN pip download --no-cache-dir --only-binary=:all: --dest /wheels -r requirements-dev.txt

FROM python:3.12-slim AS test
ENV PYTHONDONTWRITEBYTECODE=1 PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_ROOT_USER_ACTION=ignore
WORKDIR /src
COPY requirements.txt requirements-dev.txt ./
RUN --network=none --mount=type=bind,from=deps,source=/wheels,target=/wheels \
    pip install --no-cache-dir --no-index --find-links=/wheels -r requirements-dev.txt
COPY pyproject.toml ./
COPY kervan_evals ./kervan_evals
COPY scenarios ./scenarios
COPY tests ./tests
RUN --network=none python -m pytest -p no:cacheprovider && touch /tests-passed

FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_ROOT_USER_ACTION=ignore
COPY requirements.txt /tmp/requirements.txt
# Mounting /tests-passed makes this image depend on the test stage succeeding.
RUN --network=none \
    --mount=type=bind,from=deps,source=/wheels,target=/wheels \
    --mount=type=bind,from=test,source=/tests-passed,target=/tmp/tests-passed \
    pip install --no-cache-dir --no-index --find-links=/wheels -r /tmp/requirements.txt \
    && rm /tmp/requirements.txt \
    && useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin evals
WORKDIR /app
COPY kervan_evals ./kervan_evals
COPY scenarios ./scenarios
RUN --network=none python -m kervan_evals validate
USER evals
ENTRYPOINT ["python", "-m", "kervan_evals"]
CMD ["--help"]
