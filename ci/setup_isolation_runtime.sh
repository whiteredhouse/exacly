#!/bin/sh
# Explicit administrator setup for the disposable Ubuntu 24.04 CI host, never called by Exacly.
set -eu

isolation_setup_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
isolation_restriction=$(cat /proc/sys/kernel/apparmor_restrict_unprivileged_userns)
isolation_enforcement=$(cat /sys/module/apparmor/parameters/enabled)
test "$isolation_restriction" = 1
test "$isolation_enforcement" = Y

sudo apt-get update
sudo apt-get install --yes apparmor bubblewrap libseccomp2

# A future vendor profile needs review rather than a second attachment for the same binary.
for isolation_vendor_profile in /etc/apparmor.d/bwrap /etc/apparmor.d/usr.bin.bwrap /etc/apparmor.d/bubblewrap; do
  if test -e "$isolation_vendor_profile"; then
    printf 'Review the existing Bubblewrap AppArmor profile before CI setup: %s\n' "$isolation_vendor_profile" >&2
    exit 1
  fi
done

sudo install -m 0644 "$isolation_setup_dir/apparmor/exacly-bwrap" /etc/apparmor.d/exacly-bwrap
sudo apparmor_parser --replace /etc/apparmor.d/exacly-bwrap

# Neither the global restriction nor AppArmor enforcement may be relaxed by this setup.
test "$(cat /proc/sys/kernel/apparmor_restrict_unprivileged_userns)" = "$isolation_restriction"
test "$(cat /sys/module/apparmor/parameters/enabled)" = "$isolation_enforcement"
printf 'Scoped Bubblewrap profile loaded; AppArmor user namespace restriction remains %s.\n' "$isolation_restriction"
