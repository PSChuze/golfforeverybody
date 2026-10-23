# Everybody's Golf Online game servers: regi 12102, gate 12103, play 12104, website 12105 and 80.
#
# Standard library only (Blowfish is mingol/blowfish.py). Accounts and the website's posts live
# under /data, logs under /logs; compose.yml mounts both from the state folder.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    MINGOL_LOG_DIR=/logs \
    MINGOL_ACCOUNTS=/data/accounts.json

WORKDIR /app
COPY mingol /app/mingol
COPY tests /app/tests
# server-revision.json (the commit this was built from, for the panel's update notice) is
# written by the setup program and its packaging; the pattern lets a build go on without it.
COPY run.py server-revision.jso[n] /app/

# The tests gate the image: a failing suite fails the build, and the old image keeps running.
# test_setup.py checks the setup program, which is not in the image.
RUN rm /app/tests/test_setup.py && t=$(mktemp -d) && MINGOL_LOG_DIR=$t python -m unittest discover -s /app -p "test_*.py" -t /app \
    && rm -rf "$t" /app/tests && find /app -name __pycache__ -prune -exec rm -rf {} +

VOLUME ["/data", "/logs"]
EXPOSE 80 12102 12103 12104 12105

CMD ["python", "-u", "run.py"]
