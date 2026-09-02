# The PATH every privileged entry point runs with.
#
# One policy, because five copies of a security-sensitive default are five
# places for it to drift. Set rather than inherited: each of these scripts runs
# as root, and PATH decides which binary every later command in them resolves
# to. /command first among the runtime dirs so s6's tools are the image's own.
PATH=/opt/hermes/bin:/opt/hermes/.venv/bin:/command:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
export PATH
