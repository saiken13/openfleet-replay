FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app

COPY pyproject.toml ./
RUN mkdir openfleet_sim && touch openfleet_sim/__init__.py && pip install .

COPY . .
RUN pip install --no-deps .

CMD ["openfleet-sim", "--scenario", "scenarios/demo.yaml", "--loop"]
