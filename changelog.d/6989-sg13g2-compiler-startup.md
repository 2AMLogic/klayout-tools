- **The SG13G2 model installer checks the pinned compiler before compiling** (#6989, 2026-11-22).
  If the compiler cannot start, the installer reports its path and the loader’s
  diagnostic. Cached model runs continue to verify each OSDI in ngspice.
