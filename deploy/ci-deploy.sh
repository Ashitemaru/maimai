#!/bin/sh
# Forced SSH command for the CI deploy key (see deploy/README.md). Install root-owned
# 0755 as /opt/mai-journal/ci-deploy.sh. stdin carries the release archive for
# ci_receive.py; whatever command the client sends is ignored.
sudo -n -u maimai /usr/bin/python3 /opt/mai-journal/ci_receive.py || exit 1
status=0
sudo -n /usr/bin/systemctl start mai-journal-deploy.service || status=$?
sudo -n /usr/bin/journalctl -u mai-journal-deploy.service -n 60 -o cat --no-pager || true
exit "$status"
