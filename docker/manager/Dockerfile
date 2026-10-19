# syntax=docker/dockerfile:1
#
# Manager — controls the rest of the stack from inside Docker via the
# mounted host socket. Docker CLI + compose plugin + bash for the
# watchdog/manage.sh side, plus a stdlib-only Python3 for the internal
# deploy_server.py (no extra deps needed for either).

FROM docker:27-cli

RUN apk add --no-cache bash docker-cli-compose python3

COPY lib.sh entrypoint.sh watchdog.sh manage.sh strategies.map deploy_server.py /usr/local/bin/
RUN chmod +x /usr/local/bin/entrypoint.sh /usr/local/bin/watchdog.sh /usr/local/bin/manage.sh /usr/local/bin/deploy_server.py

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
