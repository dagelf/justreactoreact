# Proof Bundle

Generated: `2026-03-08T22:27:03Z`

End-to-end validation ran against a local `python -m http.server` using headless Chromium.

## Summary
- Browser: **Chromium 145.0.7632.159 Arch Linux**
- Pages validated: **2**
- Checks passed: **13 / 13**

## `index.html`
- Raw bytes: **18424 B** (17.99 KiB)
- Gzip bytes: **4267 B** (4.17 KiB)
- Browser navigation: response start **1.5 ms**, DOMContentLoaded **53.4 ms**, load **55.7 ms**
- Claim checks: **4 / 4**
  - Modal Dialog: HTML **5** / React **41** (`pass`)
  - Dark Mode Toggle: HTML **3** / React **29** (`pass`)
  - Form Validation: HTML **5** / React **23** (`pass`)
  - Accordion/Collapsible: HTML **4** / React **24** (`pass`)
- Functionality checks: **5 / 5**
  - react_code_toggles: `pass`
  - native_dialog_open_close: `pass`
  - native_form_validation: `pass`
  - native_accordion_toggle: `pass`
  - dark_mode_toggle: `pass`

## `multiuser-postgres.html`
- Raw bytes: **29376 B** (28.69 KiB)
- Gzip bytes: **7416 B** (7.24 KiB)
- Browser navigation: response start **1.6 ms**, DOMContentLoaded **8.6 ms**, load **10 ms**
- Functionality checks: **4 / 4**
  - react_code_toggles: `pass`
  - simulate_db_query: `pass`
  - simulate_react_updates: `pass`
  - measure_bundle_impact: `pass`

## Reproduce
```bash
python proof/generate_proof.py
```
