// Declared include/header data for the #2097 identity-contract fixtures.
//
// The v1 contract does *not* hash include directories: include/header bytes
// enter the manifest only through the caller's explicit `files` inventory
// (role `rtl_include`), issue #2097, "Request surface and inventory".
`define COUNTER_DEFAULT_WIDTH 8
