#!/bin/sh
# Forced SSH command for the CI deploy key (see deploy/README.md). Install root-owned
# 0755 as /opt/mai-journal/ci-deploy.sh. Whatever command the client sends is ignored.
status=0
sudo -n /usr/bin/systemctl start mai-journal-deploy.service || status=$?
sudo -n /usr/bin/journalctl -u mai-journal-deploy.service -n 60 -o cat --no-pager || true
exit "$status"
