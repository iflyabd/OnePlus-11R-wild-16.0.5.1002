# Firmware baseline: CPH2487_16.0.5.1002 (EX01)

- OOS 16.0.5.1002, Android 16, security patch July 1 2026, baseband Q_V1_P14
- Kernel 5.10.236-android12-OP-WILD (Clang 12.0.6), KSU manager v3.3.0 on device
- Active slot at capture: `_a`

## Slot policy
- `boot_a` is sacred: working fallback, never overwritten by tests.
- All test flashes go to `boot_b`, hash-verified before and after.

## Sealed backups (see release `base-16.0.5.1002`)
- `BOOT_A_16.0.5.1002_backup.img` — pristine working dump, never flash over it lightly
- `BOOT_A_16.0.5.1002_custom-base.img` — working base for custom kernel repacks
- Both sha256 `65a102be384b3e680be12fa15e3146cd9f4a7a7573848eadc401be742ab6c126`
