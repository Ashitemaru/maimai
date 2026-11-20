#!/bin/sh
# Forced SSH command for the CI deploy key (see deploy/README.md). Install root-owned
# 0755 as /opt/mai-journal/ci-deploy.sh. The client may only ask for:
#   wheels  print digests of cached dependency wheels
#   deploy  read SHA line + release archive on stdin, then run the deployer
receive="sudo -n -u maimai /usr/bin/python3 /opt/mai-journal/ci_receive.py"
case "$SSH_ORIGINAL_COMMAND" in
wheels)
	exec $receive list
	;;
deploy)
	sha=$($receive upload) || exit 1
	status=0
	sudo -n /usr/bin/systemctl start mai-journal-deploy.service || status=$?
	sudo -n /usr/bin/journalctl -u mai-journal-deploy.service -n 60 -o cat --no-pager || true
	[ "$status" -eq 0 ] || exit "$status"
	# systemctl start joins a deployment that is already running; confirm ours is live.
	current=$($receive current)
	if [ "$current" != "$sha" ]; then
		echo "Active release is $current, not the uploaded $sha; rerun this deploy job" >&2
		exit 1
	fi
	echo "Active release is $sha"
	;;
*)
	echo "Unsupported command; use wheels or deploy" >&2
	exit 2
	;;
esac
