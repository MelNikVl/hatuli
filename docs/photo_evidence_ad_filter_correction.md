# Photo-evidence canary: ad-image correction

Date: 2026-08-18

The two Krisha advertisement references were re-audited before any further
photo-evidence coverage work.

| Reference | Original SHA-256 | pHash | CDN fingerprint rows |
| --- | --- | --- | --- |
| 1 | `db30b8758249cf797d8df5afe308ef91b8dae2c5f863d486dc6b6b4c3a280862` | `f8f4cf81dc17200f` | 200 |
| 2 | `76d0d8ef35582c03ec57fc74a4fcfd6ca942093d94b9cfb344639ba955fc6bfa` | `e0ce2517dbe40ae9` | 223 |

All CDN rows have pHash Hamming distance zero to their reference. They map to
two shared CDN URLs and 423 listing-photo occurrences. Their bytes are
re-encoded by the CDN, hence their SHA-256 differs from the originals.

The existing SigLIP metadata had labelled the first family `other` and the
second `render` using `siglip_vit_b16_webli_v1`; the URL block-list carried
the original `siglip_etalon` reason and scores in the 0.988--0.996 range.
Per-image classifier confidence is not persisted in `listing_photo_fingerprints`.

## Effect and correction

The filter now excludes these images before exact, perceptual, or AI matching.
Existing cleanup removed the 423 URLs from `apartment_listings.photos`, retained
raw-cache/fingerprint audit data where present, and offline-reaggregated the 20
affected candidate evidence rows. No photo-derived candidate edges exist.

The published canary aggregate tallies must be treated as **preliminary** until
the canary summary is regenerated from the cleaned evidence. The affected rows
have been excluded without a second SigLIP pass; a future summary regeneration
is a reporting operation, not an AI rerun.
