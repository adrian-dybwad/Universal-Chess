# Release signing keyring

This directory ships the **public** half of the release signing key, as a binary
GnuPG keyring named `release-signing.gpg`. It is installed root-owned to
`/opt/universalchess/keys/release-signing.gpg`.

## What it is for

`scripts/install-update` runs as root through a passwordless sudo grant and
installs a `.deb` that the update service downloaded into
`/opt/universalchess/pending-updates/`. That directory is writable by the service
user, so nothing in it can be trusted on its own — including the checksum
manifest. Verifying a checksum supplied by the same party that supplied the
package proves nothing.

This keyring is the trust anchor that fixes that. It lives in the root-owned
install tree, so the service user cannot replace it. The helper uses it to verify
a detached signature over `SHA256SUMS.txt`, and only then compares the package's
SHA-256 against that now-trusted manifest.

The helper verifies with `sqv` or `gpgv`, whichever the board has, rather than
with full `gpg`: both check against a fixed keyring without mutating it and need
no `GNUPGHOME`, which is the same job apt does. Supporting either matters because
the guaranteed tool differs by Debian release — bookworm's `apt` depends on
`gpgv`, while trixie's is built against Sequoia and depends on `sqv` instead. The
package declares `sqv | gpgv`, so the dependency is already satisfied on both and
an update never has to fetch a verifier over the network to verify itself.

## Required setup

The private key is **not** in this repository and must never be committed.

1. Generate a signing key (once), on a machine you control:

   ```sh
   gpg --quick-generate-key "Universal Chess Release Signing" ed25519 sign never
   ```

2. Export the public certificate into this directory:

   ```sh
   gpg --export "Universal Chess Release Signing" \
     > packaging/deb-root/opt/universalchess/keys/release-signing.gpg
   ```

   **Use `--export` exactly as above.** It writes a plain OpenPGP certificate
   stream, which both `sqv` and `gpgv` can read. Do *not* build the file with
   `gpg --no-default-keyring --keyring <file> --import`: modern GnuPG creates a
   *keybox* there, and `sqv` cannot parse that format — so verification would
   work on bookworm and fail on every trixie board. Confirm the format before
   committing:

   ```sh
   file packaging/deb-root/opt/universalchess/keys/release-signing.gpg
   # want: "OpenPGP Public Key"   NOT: "GPG keybox database"
   ```

3. Export the private key and store it as the GitHub Actions secret
   `RELEASE_SIGNING_KEY`, with its passphrase (if any) as
   `RELEASE_SIGNING_PASSPHRASE`:

   ```sh
   gpg --armor --export-secret-keys "Universal Chess Release Signing"
   ```

4. Keep an offline backup of the private key. Losing it means boards running a
   release signed by it can no longer verify newer releases, and recovering
   requires a manually installed `.deb` to replace the keyring.

## Why the build fails without it

`scripts/build.sh` refuses to build a package when this keyring is missing. A
package without it would install successfully and then be unable to verify any
future update, so every subsequent OTA would be refused — a board that can no
longer update itself, recoverable only by hand. Failing at build time keeps that
failure in CI instead of in the field.

## Rotating the key

Add the new public key to the keyring **before** signing with it, and ship that
release first. Boards must already trust the new key by the time a release signed
only by it arrives:

```sh
gpg --export "old-key-id" "new-key-id" \
  > packaging/deb-root/opt/universalchess/keys/release-signing.gpg
```

Once every supported board is on a release carrying both keys, the old one can be
dropped.
