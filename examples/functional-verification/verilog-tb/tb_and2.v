`timescale 1ns/1ps
// Self-checking bench: prints PASS/FAIL and finishes; klt derives the verdict.
module tb_and2;
  reg a, b;
  wire y;
  integer errors = 0;
  and2 dut(.a(a), .b(b), .y(y));
  task check(input exp);
    begin
      #5;  // longer than any annotated delay
      if (y !== exp) begin
        $display("FAIL: a=%b b=%b y=%b want %b", a, b, y, exp);
        errors = errors + 1;
      end
    end
  endtask
  initial begin
    a = 0; b = 0; check(0);
    a = 1; b = 0; check(0);
    a = 1; b = 1; check(1);
    if (errors == 0) $display("PASS");
    $finish;
  end
endmodule
