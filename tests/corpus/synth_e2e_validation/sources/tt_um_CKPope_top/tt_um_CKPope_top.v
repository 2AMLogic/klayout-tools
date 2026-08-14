module tt_um_CKPope_top 
(
    input  wire [7:0] ui_in,    // Dedicated inputs for X and Y Target coordinates (BCD: 7:4 for X; 3:0 for Y)
    output wire [7:0] uo_out,   // Dedicated outputs for X Position and Y Position (BCD: 7:4 for X; 3:0 for Y)
    input  wire [7:0] uio_in,   // IOs: Input path for FSM Control MOTION control input plus spares
    output wire [7:0] uio_out,  // IOs: Output path (not used)
    output wire [7:0] uio_oe,   // IOs: Enable path (active high: 0=input, 1=output) - TIED For UIO BEING INPUTS ONLY
    input  wire       ena,      // will go high when the design is enabled
    input  wire       clk,      // clock
// ---------------------------------------------------------------
//outputs for simulation diagnostics
//	output wire	init_regs_out,
//	output wire load_xy_out,
//	output wire [3:0] x_target_reg_out,
//	output wire [3:0] y_target_reg_out,
//	output wire x_comp_eq_out, x_comp_gt_out, x_comp_lt_out,
//	output wire x_counten_out, x_count_up1_dwn0_out,
//	output wire y_comp_eq_out, y_comp_gt_out, y_comp_lt_out,
//	output wire y_counten_out, y_count_up1_dwn0_out,
//	output wire reset_n_out, reset_out, motion_out,
//-- --------------------------------------------------------------
   input  wire rst_n     // reset_n - low to reset
);// Quartus Prime Verilog Template

//----------------------------------------------------------------
//Local tile signal Inputs
wire clock, sync_reset, reset_n;

//FSM Initialization of external registers
wire init_regs;

//FSM control inputs;
wire motion_inp,sync_motion;

// x-y target data inputs
wire [3:0] x_target;
wire [3:0] y_target;

// x-y target_register signals
wire [3:0] x_target_reg;
wire [3:0] y_target_reg;
wire load_xy;

// x-y counter signals
wire x_counten, x_count_up1_dwn0, y_counten, y_count_up1_dwn0;
wire [3:0] x_pos;
wire [3:0] y_pos;

// X-Y Counter comparator signals
wire x_comp_eq, x_comp_gt, x_comp_lt;
wire y_comp_eq, y_comp_gt, y_comp_lt;

//----------------------------------------------------------------
// make Connections to infrastructure signals. clock can only run when the module is enabled (selected)
assign clock 	= clk;
assign reset_n =  rst_n;

//----------------------------------------------------------------
// make Connections to uio, ui, uo ports
assign motion_inp = (uio_in[0]);
assign x_target = ui_in[7:4];
assign y_target = ui_in[3:0];
assign uo_out[7:4] = x_pos[3:0];
assign uo_out[3:0] = y_pos[3:0];

// TIE OFF Unused uio ports
assign uio_oe = 8'b0;
assign uio_out = 8'b0;
//----------------------------------------------------------------
// module instantiation

// Input Synchronizer
input_synch buttons
(.clk (clock), .reset(!reset_n), .sync_reset(sync_reset), .sync_in(motion_inp),  .sync_out (sync_motion));

//FSM:
Mealy_SM xycontroller 
(.clk(clock), .reset(!reset_n | sync_reset), .motion(sync_motion), .init(init_regs),
.x_comp_eq(x_comp_eq), .x_comp_gt(x_comp_gt), .x_comp_lt(x_comp_lt), .x_count_en(x_counten), .x_up1_dwn0(x_count_up1_dwn0),
.y_comp_eq(y_comp_eq), .y_comp_gt(y_comp_gt), .y_comp_lt(y_comp_lt), .y_count_en(y_counten), .y_up1_dwn0(y_count_up1_dwn0), 
.capt_enbl(load_xy));

//X, Y Target Capture Registers
target_reg x_treg (.clk (clock), .load (load_xy), .data (x_target), .reset ((sync_reset | init_regs)), .target_reg (x_target_reg));
target_reg y_treg (.clk (clock), .load (load_xy), .data (y_target), .reset ((sync_reset | init_regs)), .target_reg (y_target_reg));	 	

//X, Y Counters:
ud_counter x_counter (.clk (clock), .count_en (x_counten), .count_up1_dwn0 (x_count_up1_dwn0), .reset ((sync_reset | init_regs)), .count (x_pos[3:0]));
ud_counter y_counter (.clk (clock), .count_en (y_counten), .count_up1_dwn0 (y_count_up1_dwn0), .reset ((sync_reset | init_regs)), .count (y_pos[3:0]));	 	

// X, Y Comparators
Compx4 x_comp (.a_hex(x_pos), .b_hex(x_target_reg), .a_eq_b(x_comp_eq), .a_gt_b(x_comp_gt), .a_lt_b(x_comp_lt)); 
Compx4 y_comp (.a_hex(y_pos), .b_hex(y_target_reg), .a_eq_b(y_comp_eq), .a_gt_b(y_comp_gt), .a_lt_b(y_comp_lt)); 

//----------------------------------------------------------------
// temporary connections for diagnostics outputs
//assign x_target_reg_out = 	x_target_reg;
//assign y_target_reg_out = 	y_target_reg;
//assign load_xy_out = load_xy;
//assign x_counten_out = x_counten; 
//assign x_comp_eq_out = x_comp_eq; 
//assign x_comp_gt_out = x_comp_gt; 
//assign x_comp_lt_out = x_comp_lt;
//assign x_count_up1_dwn0_out = x_count_up1_dwn0;
//assign y_counten_out = y_counten; 
//assign y_comp_eq_out = y_comp_eq; 
//assign y_comp_gt_out = y_comp_gt; 
//assign y_comp_lt_out = y_comp_lt;
//assign y_count_up1_dwn0_out = y_count_up1_dwn0;
//assign reset_out = sync_reset;
//assign reset_n_out = reset_n;
//assign init_regs_out = init_regs;
//assign motion_out= sync_motion;

endmodule


