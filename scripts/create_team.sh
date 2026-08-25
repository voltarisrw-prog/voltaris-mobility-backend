#!/usr/bin/env bash
#
# Create one account per role.
#
#     bash scripts/create_team.sh
#
# Passwords are generated and printed once. Copy them into a password manager
# before closing the terminal — they are not recoverable, only resettable.
#
# There is no STAFF role. "Staff" in the system means one of SALES_AGENT,
# FINANCE, or CONTENT_MANAGER, which have deliberately different powers:
# FINANCE can refund a payment, SALES_AGENT cannot.

set -uo pipefail
cd "$(dirname "$0")/.." || exit 1

PY="${PYTHON:-python}"
run(){ "$PY" scripts/create_user.py --email "$1" --name "$2" --roles "$3"; }

echo "Creating one account per role."
echo "Copy each password before this scrolls away."
echo

run buyer@voltaris.rw            "Test Buyer"          BUYER
run seller@voltaris.rw           "Test Seller"         SELLER
run dealer@voltaris.rw           "Test Dealer"         DEALER
run sales@voltaris.rw            "Sales Agent"         SALES_AGENT
run finance@voltaris.rw          "Finance Officer"     FINANCE
run content@voltaris.rw          "Content Manager"     CONTENT_MANAGER

# The two real people.
run patrice.iradukunda@aims.ac.rw "Patrice Iradukunda" ADMIN
run voltaris.rw@gmail.com         "Voltaris Owner"     SUPER_ADMIN

cat <<'NOTE'

  Done.

  ADMIN does NOT include: payment:refund, commission:write, settlement:write,
  role:assign, config:write, system:inspect. Moving money and granting privilege
  are separated from general administration on purpose. If Patrice needs to
  refund payments, add FINANCE alongside ADMIN:

    python scripts/create_user.py --email patrice.iradukunda@aims.ac.rw \
      --name "Patrice Iradukunda" --roles ADMIN,FINANCE --update-existing

  Neither account has a second factor — MFA is modelled but not implemented.
  Until it is, the SUPER_ADMIN password is the only thing protecting every
  record in the system. Use a long generated one and store it properly.

NOTE
