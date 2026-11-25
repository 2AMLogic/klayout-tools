- **`klt em` adds steady heat and Joule self-heating on tagged meshes** (#7892).
  Both modes return nodal temperatures and a heat balance in watts.
  Self-heating reports Picard iteration exhaustion and runaway as errors.
  These modes require a Geode CLI that includes geode-fem#1806.
