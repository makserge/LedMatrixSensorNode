FROM ghcr.io/home-assistant/base-python:3.13-alpine3.24

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    S6_KILL_GRACETIME=10000 \
    S6_SERVICES_GRACETIME=10000

COPY requirements.txt /tmp/requirements.txt
RUN pip3 install --no-cache-dir --require-hashes -r /tmp/requirements.txt && rm /tmp/requirements.txt

WORKDIR /app
COPY radar_zone_learner /app/radar_zone_learner
COPY run.sh /run.sh
RUN chmod a+x /run.sh

CMD [ "/run.sh" ]
