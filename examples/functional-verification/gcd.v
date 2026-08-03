// Minimal iterative-subtractor GCD core -- the worked example from
// docs/design/cocotb-verification-spike.md section 6, verbatim.
//
// Handshake: assert start with a,b valid for one cycle; done pulses
// high for one cycle when result is valid.
//
// Note the `a == 0` exit condition alongside the textbook `b == 0` one: it
// is load-bearing, not redundant. With `a == 0`, `a > b` is always false, so
// a core checking only `b == 0` takes the `b <= b - a` branch forever
// (`b - 0 == b`) and hangs -- a real bug the spike's own testbench caught
// live (spike section 6, "A real RTL bug, found and fixed").
module gcd #(
    parameter WIDTH = 16
) (
    input  wire             clk,
    input  wire             rst_n,
    input  wire             start,
    input  wire [WIDTH-1:0] a_in,
    input  wire [WIDTH-1:0] b_in,
    output reg              done,
    output reg  [WIDTH-1:0] result
);

  reg [WIDTH-1:0] a, b;
  reg busy;

  always @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      a      <= {WIDTH{1'b0}};
      b      <= {WIDTH{1'b0}};
      busy   <= 1'b0;
      done   <= 1'b0;
      result <= {WIDTH{1'b0}};
    end else begin
      done <= 1'b0;
      if (start && !busy) begin
        a    <= a_in;
        b    <= b_in;
        busy <= 1'b1;
      end else if (busy) begin
        if (a == {WIDTH{1'b0}}) begin
          result <= b;
          done   <= 1'b1;
          busy   <= 1'b0;
        end else if (b == {WIDTH{1'b0}}) begin
          result <= a;
          done   <= 1'b1;
          busy   <= 1'b0;
        end else if (a > b) begin
          a <= a - b;
        end else begin
          b <= b - a;
        end
      end
    end
  end

endmodule
