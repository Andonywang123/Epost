# Xiaohongshu platform notes

Read this reference when reviewing platform limits or recalibrating the local adapter.

## Product decision

- This skill is single-machine and file-folder based.
- The publishing engine is a visible, long-running Chrome connected through Playwright CDP with a dedicated persistent profile. Playwright-managed launch is only a fallback.
- It uses neither a publishing API nor object storage, a database, copied cloud cookies, password automation, CAPTCHA bypass, or reverse-engineered signatures.

## Observed web capabilities

- Image post: up to 18 images; common image formats; each image up to 32 MB in the tested creator UI.
- Title: up to 20 characters. Body: up to 1000 characters in the tested UI.
- Visibility options included public, private, mutual friends, selected people, and excluded people.
- Topic suggestions, activities, location, group, live-preview and other creator components may appear according to account and post context.
- Submitting a post can lead to an `UNDER_REVIEW` state. Publication completion must be checked separately.
- Web draft storage may be tied to the current browser. The local post folder remains the durable source of truth.

These are tested UI observations, not a stable public API contract. Revalidate them when the creator page changes.
