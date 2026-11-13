#!/usr/bin/env bash
# CPU-only behavioral guard: host CLI state must reach a separately compiled engine reader.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
ZIG="${ZIG:-$ROOT/.zig-toolchain/zig}"
WORK="$ROOT/.build-cache/os-reserve-units"
mkdir -p "$WORK"
ln -sfn "$ROOT/src" "$WORK/source"
export ZIG_GLOBAL_CACHE_DIR="${ZIG_GLOBAL_CACHE_DIR:-$ROOT/.build-cache/zig-global}"
export ZIG_LOCAL_CACHE_DIR="$WORK/cache"
cat > "$WORK/engine.zig" <<'ZIG'
pub const mlx_serve_unit = true;
const system = @import("source/sdk/util/system.zig");
export fn engineReserve(total: u64, device: bool) u64 {
    return system.osReserveBytes(total, device);
}
export fn engineLoadRoom(total: u64, wired: u64, compressor: u64, page: u64) u64 {
    return system.computeLoadAvailableBytes(total, wired, compressor, page, system.osReserveBytes(total, false));
}
ZIG
cat > "$WORK/host.zig" <<'ZIG'
const std = @import("std");
const system = @import("source/sdk/util/system.zig");
extern fn engineReserve(u64, bool) u64;
extern fn engineLoadRoom(u64, u64, u64, u64) u64;
test "OS reserve crosses host/engine compilation units" {
    const gib: u64 = 1 << 30;
    defer system.setOsReserveOverride(null);
    try std.testing.expectEqual(8 * gib, engineReserve(256 * gib, false));
    system.setOsReserveOverride(try system.parseOsReserveGib("24"));
    try std.testing.expectEqual(24 * gib, engineReserve(256 * gib, false));
    try std.testing.expectEqual(24 * gib, engineReserve(256 * gib, true));
    try std.testing.expectEqual(200 * gib, engineLoadRoom(256 * gib, 30 * gib / 4096, 2 * gib / 4096, 4096));
    try std.testing.expectEqual(@as(u64, 0), engineLoadRoom(16 * gib, 0, 0, 4096));
    system.setOsReserveOverride(try system.parseOsReserveGib("0"));
    try std.testing.expectEqual(@as(u64, 0), engineReserve(256 * gib, false));
    try std.testing.expectEqual(224 * gib, engineLoadRoom(256 * gib, 30 * gib / 4096, 2 * gib / 4096, 4096));
    system.setOsReserveOverride(try system.parseOsReserveGib("64"));
    try std.testing.expectEqual(64 * gib, engineReserve(256 * gib, false));
    for ([_][]const u8{ "65", "-1", "1.5", "lots", "4294967296" }) |bad|
        try std.testing.expectError(error.InvalidOsReserve, system.parseOsReserveGib(bad));
    system.setOsReserveOverride(null);
    try std.testing.expectEqual(2 * gib, engineReserve(8 * gib, false));
    try std.testing.expectEqual(4 * gib, engineReserve(32 * gib, false));
    try std.testing.expectEqual(8 * gib, engineReserve(256 * gib, false));
    try std.testing.expectEqual(@as(u64, 0), engineReserve(256 * gib, true));
}
ZIG
"$ZIG" build-obj -O ReleaseFast -Mroot="$WORK/engine.zig" -femit-bin="$WORK/engine.o"
"$ZIG" test --test-filter "OS reserve crosses" -O ReleaseFast -lc "$WORK/engine.o" -Mroot="$WORK/host.zig"
