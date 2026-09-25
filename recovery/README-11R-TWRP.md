# Recovery slot: TWRP for OnePlus 11R (udon / Ace 2)

No TWRP image is bundled here yet (drop `twrp-udon.img` next to the kernel
zips in the release to fill this slot).

Known source: unofficial TWRP 3.7.0 for 11R/Ace 2 (Chinese dev lineage,
mirrored on unofficialtwrp.com). A14-era build.

Rules learned from testers:
- KEEP vbmeta verity/verification ON, or the system won't boot.
- Chinese UI before data unlock: switch language after decrypt.
- No A16 data decryption expected from an A14-era build.

Flash (needs unlocked bootloader + PC fastboot):
  fastboot flash recovery_a twrp-udon.img
  fastboot flash recovery_b twrp-udon.img
  # or test without flashing:
  fastboot boot twrp-udon.img
