# Payments

## The rule

The frontend cannot make a payment succeed. There is no endpoint that accepts a
payment status from a client, and no code path where a client-supplied number
influences an amount. This is verified by `test_frontend_cannot_declare_a_payment_successful`,
which attempts three plausible shapes of that attack and asserts all three 404 or 405.

## The flow

```
customer                backend                         provider
   |                       |                                |
   |-- POST /orders ------>|  price read from the vehicle    |
   |                       |  vehicle reserved atomically    |
   |<-- 201 order ---------|                                 |
   |                       |                                 |
   |-- POST checkout ----->|  amount read from the order     |
   |                       |-- create session -------------->|
   |<-- redirect_url ------|<------------------------------- |
   |                                                         |
   |------------------ pays on the provider's page --------->|
   |                       |                                 |
   |                       |<-- signed webhook ------------- |
   |                       |  six gates                      |
   |                       |  PAID -> order PAID             |
   |                       |  -> vehicle SOLD                |
   |                       |  -> commission booked           |
   |                       |                                 |
   |-- GET payment ------->|  reconciled state, never cached |
```

The customer's return redirect proves nothing and is not treated as evidence. The
frontend polls `GET /orders/{id}/payment`, which reads the state the webhook wrote.

## The six gates

Every one is a separate check and every failure is recorded in `payment_events`.

| # | Gate | Failure |
| --- | --- | --- |
| 1 | HMAC signature over the raw bytes, with the timestamp inside the signed material | 401 `WEBHOOK_SIGNATURE_INVALID` |
| 2 | Replay: read check, then a unique index on `(provider, provider_event_id)` | 200 `duplicate` — the provider must stop retrying |
| 3 | The payment exists | 404 `PAYMENT_NOT_FOUND` |
| 4 | Reported amount equals the recorded amount | 409 `PAYMENT_AMOUNT_MISMATCH`, audited |
| 5 | Reported currency equals the recorded currency | 409 `PAYMENT_CURRENCY_MISMATCH` |
| 6 | The transition is legal in the state machine | 409 `INVALID_STATE_TRANSITION` |

Gate 1 uses the raw request body, not a re-serialised dict — re-encoding JSON changes
whitespace and key order and would break the digest.

Gate 2 is deliberately two layers. The read catches ordinary redelivery cheaply; the
unique index catches two deliveries racing, where both reads miss before either write
lands. Neither is sufficient alone.

Gate 6 is why `PAID -> PAID` is not a legal transition. Even if replay protection were
bypassed entirely, a second success event cannot re-book revenue.

## Amount tampering

A correctly signed webhook claiming the customer paid 100 RWF for a 27,000,000 RWF
vehicle is rejected at gate 4, the order stays unpaid, no commission is written, and
`payment.amount_mismatch` is audited. Tested.

## The commission split

Integer minor units throughout. Floats cannot represent 0.1 exactly, and a settlement
computed in floats drifts until it stops reconciling against the provider.

```
gross_sale        = 27,000,000     what the customer paid
owner_settlement  = 25,000,000     what the seller was promised, taken literally
agency_commission =  2,000,000     the remainder
payment_fees      =    783,000     2.9% of gross
net_revenue       =  1,217,000     commission minus fees
```

Invariant, asserted before anything is written:
`owner_settlement + agency_commission == gross_sale`.

Payment fees come out of Voltaris's commission, never out of the seller's settlement.
The seller was quoted a number.

Two conditions raise rather than compute: a seller expectation above the gross sale,
and a spread thinner than the agreed commission rate. Both are upstream pricing
mistakes and must surface rather than quietly erode margin.

### When the split cannot be computed

The customer has already paid. Refusing at that point would 500 the webhook, trigger
endless provider redelivery, and leave a paid order with no financial record at all.

So the sale is booked with status `NEEDS_REVIEW`, an empty split, the reason attached,
an `ERROR` log line, and a `commission.needs_review` audit entry. Finance resolves it
by hand. The money is never lost, only unallocated.

This behaviour exists because the first run of the test suite crashed here: the
commission floor fired on a settled payment and took the webhook down with it. The
guard was right; the failure handling was wrong.

## Provider integration

**No real provider is wired up.** `PaymentProvider` is the interface an adapter must
satisfy. `StubProvider` implements it for development and cannot mark anything paid —
advancing a payment still requires a correctly signed webhook, so the development path
exercises the same gates as production.

`get_provider()` raises `NOT_CONFIGURED` if the provider is still `stub` in
production. A stub in production would accept orders that can never be paid.

To integrate a real provider, implement the protocol, add the branch in
`get_provider()`, and set `PAYMENT_API_KEY` and `PAYMENT_WEBHOOK_SECRET`. If the
provider's signature scheme differs from `t=<unix>,v1=<hex>`, replace
`verify_webhook_signature` — it is the only place that format is known.

## Refunds

`PAID -> REFUNDED` is modelled and permitted in the state machine. The initiating
endpoint is not built: it needs `PAYMENT_REFUND`, which only FINANCE and SUPER_ADMIN
hold — deliberately not ADMIN.
