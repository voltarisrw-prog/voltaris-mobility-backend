# Images

## The flow

```
browser                    API                       R2
   |                        |                         |
   |-- POST /media/intents->|  validates the claims   |
   |                        |  presigns one PUT each  |
   |<-- upload_url ---------|                         |
   |                                                  |
   |------------ PUT the file directly -------------->|  quarantine/
   |                                                  |
   |-- POST /media/finalize>|                         |
   |                        |<-- download original ---|
   |                        |  decode, validate,      |
   |                        |  re-encode 4 variants   |
   |                        |--- put variants ------->|  vehicles/
   |                        |--- delete original ---->|
   |<-- VehicleImage[] -----|                         |
```

**Originals never pass through the API server.** A 12 MB upload therefore never
occupies a worker, and a malicious file never lands on the machine running the
business logic.

**The limits are enforced by R2, not by trust.** `ContentType` and
`ContentLength` are signed *into* the presigned URL, so a client that lies about
either is rejected by R2 before a byte is stored. The check lives in the
signature, not in a validation branch someone could forget to write.

## Why R2

Egress is free. For an image-heavy marketplace serving Rwanda through a global
CDN, egress is the line item that actually grows — S3 bills per GB out, R2 bills
nothing. The bucket speaks the S3 API, so `boto3` works unchanged and moving to
S3 later means changing an endpoint URL.

Signing uses boto3 rather than hand-rolled SigV4. Getting request signing subtly
wrong fails silently and only against the real service, which is exactly the
class of bug not to invent.

## Why every image is re-encoded

Nothing is passed through. Decoding to pixels and writing back does three jobs at
once:

**It strips EXIF.** This is the important one, and it is a privacy failure rather
than a security one, which is why it is easy to miss. A private seller
photographs their car in their driveway; the EXIF carries GPS coordinates.
Publishing the file unchanged publishes their home address. Tested in
`test_gps_coordinates_are_stripped`.

**It kills polyglots.** A file that is simultaneously a valid JPEG and a valid
archive does not survive being decoded to pixels. Tested.

**It normalises.** Sellers upload 4:3, 16:9, portrait, and rotated-by-EXIF
photos. `thumb`, `card`, and `detail` are cropped to a single 4:3 ratio so the
card grid never has a ragged edge; `gallery` keeps the whole frame, because the
detail page shows the photograph rather than a crop of it.

The EXIF orientation flag is applied and then discarded. Phones record "rotate
90°" instead of rotating pixels: ignore it and every portrait photo appears on
its side; keep the tag after resizing and the rotation applies twice.

## Validation

| Check | Guards against |
| --- | --- |
| Magic bytes, not `content_type` | The declared type is a client claim. The file is the file. |
| `MAX_IMAGE_PIXELS = 50M` | A 60 MB PNG decompressing to gigabytes of pixels |
| 400px minimum | Thumbnails passed off as photographs |
| 8000px maximum | Guards the worker — decoding is the expensive part |
| Random object keys | User filenames mean path traversal, collisions between two sellers' `IMG_1234.jpg`, and guessable URLs |
| Ownership on finalize | A media key is a bearer reference; without the check anyone could attach someone else's upload to their listing |

**No automated content moderation**, deliberately. It costs per image and there
is already a human gate — listings do not auto-publish, a reviewer sees every one
before it goes live. Add moderation when volume outgrows the reviewer, not
before.

## Variants

| Name | Size | Treatment |
| --- | --- | --- |
| `thumb` | 160×120 | cropped 4:3 |
| `card` | 640×480 | cropped 4:3 |
| `detail` | 1280×960 | cropped 4:3 |
| `gallery` | 1920×1440 max | full frame, fitted |

All WebP at quality 78. Plus a ~200-byte blur placeholder inlined as a data URI,
kept at 20px wide because it ships inside the HTML for every card in the grid —
any larger and the base64 costs more than it saves.

Variants are served with `Cache-Control: public, max-age=31536000, immutable`.
Safe because keys carry a random id: a replaced image is a new key, never an
overwritten one.

## Running it inline

`finalize` decodes on the request path. At this volume a handful of 12 MB decodes
is acceptable and keeps the failure visible to the person waiting. It is the
first thing to move to a queue when listings arrive faster than a reviewer can
look at them — the service interface does not change, only the caller.

## Setup

1. Create an R2 bucket and an API token with object read/write.
2. Put a custom domain in front of it (`media.voltaris.rw`).
3. Set `R2_*` in `.env`. **`R2_PUBLIC_BASE_URL` must match `remotePatterns` in
   the frontend's `next.config.mjs`**, or `next/image` refuses to load anything.
4. Add a lifecycle rule deleting `quarantine/` after 24 hours. Finalize deletes
   originals itself; the rule catches intents that were presigned and abandoned.

Blank credentials disable uploads cleanly — the endpoints return
`NOT_CONFIGURED` rather than presigning against a bucket that is not there.
