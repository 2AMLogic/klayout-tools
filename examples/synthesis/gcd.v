// Register-transfer-level GCD (subtractive Euclidean algorithm),
// parametrized 16-bit width, with a start/done handshake.
//
// This is the exact worked-example RTL from
// docs/design/yosys-synthesis-spike.md §4 -- kept here as a checked-in
// fixture so `scripts/verify-yosys-pin.py` (issue #417) can reproduce that
// section's `stat -json` output (num_cells/area/num_cells_by_type) against
// a pinned, reproducible Yosys build in CI, and so a future `klt synthesize`
// (issue #416) has a small, real multi-state design to exercise beyond a
// single-gate toy.
module gcd #(
    parameter WIDTH = 16
) (
    input  wire             clk,
    input  wire             rst_n,
    input  wire              start,
    input  wire [WIDTH-1:0] a_in,
    input  wire [WIDTH-1:0] b_in,
    output reg              done,
    output reg  [WIDTH-1:0] result
);

    reg [WIDTH-1:0] a, b;
    reg             busy;

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            a      <= {WIDTH{1'b0}};
            b      <= {WIDTH{1'b0}};
            busy   <= 1'b0;
            done   <= 1'b0;
            result <= {WIDTH{1'b0}};
        end else if (start && !busy) begin
            a      <= a_in;
            b      <= b_in;
            busy   <= 1'b1;
            done   <= 1'b0;
        end else if (busy) begin
            if (b == {WIDTH{1'b0}}) begin
                busy   <= 1'b0;
                done   <= 1'b1;
                result <= a;
            end else if (a > b) begin
                a <= a - b;
            end else begin
                b <= b - a;
            end
        end else begin
            done <= 1'b0;
        end
    end

endmodule
