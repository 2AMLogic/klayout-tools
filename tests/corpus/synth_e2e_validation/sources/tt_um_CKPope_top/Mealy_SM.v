module Mealy_SM
(
  input  clk,
  input  reset,	 
  input  motion,
  input  x_comp_eq,
  input	x_comp_gt,
  input  x_comp_lt,
  			 
  input  y_comp_eq,
  input	y_comp_gt,
  input  y_comp_lt,
  
  output reg init,
  output reg x_count_en,
  output reg x_up1_dwn0,
  output reg y_count_en,
  output reg y_up1_dwn0,
  output reg capt_enbl		 
);

  parameter INITIALIZE1 =3'b000,
				INITIALIZE2 =3'b001,
				AT_REST		=3'b010,
				CAPTURE_XY	=3'b011,
				IN_MOTION	=3'b100,
				XY_REACHED	=3'b101;

 // RANGE of STATE VALUES (INITIALIZE, AT_REST, CAPTURE_XY, IN_MOTION, XY_REACHED);

   reg [2:0] current_state, next_state;		// current_state, next_state registers of type STATES


// STATE MACHINE: MEALY Type

//REGISTER SECTION OF STATE MACHINE

always @(posedge clk, posedge reset) 	// sequential logic to latch the FSM state (FSM REGISTER SECTION)
   begin
		if (reset==1) 
	      current_state <= INITIALIZE1;
		else   
         current_state <= next_state; 	// on the rising edge of clock the current state is updated with next state value
   end	

	//TRANSITION SECTION OF STATE MACHINE
always @(*) 		 							// logic to determine next state. (FSM TRANSITION SECTION)
begin
     case (current_state)	  
				INITIALIZE1: 
				begin		
               next_state = INITIALIZE2;
            end
				
				INITIALIZE2: 
				begin		
               next_state = AT_REST;
            end
				
				AT_REST: 
				begin		
					if (motion) 
               next_state = CAPTURE_XY;
					else 
               next_state = AT_REST;
            end
				
				CAPTURE_XY: 
				begin		
					if (motion) 
               next_state = CAPTURE_XY; // stay here until MOTION goes off. Then go to IN_MOTION
					else
               next_state = IN_MOTION;
            end
				
				IN_MOTION:
				begin
					if ((x_comp_eq) & (y_comp_eq))
               next_state = XY_REACHED;
					else
					next_state = IN_MOTION;
            end

           XY_REACHED:
			  begin
					next_state = AT_REST;
				end
							 
			  default:
               next_state = AT_REST;
 		endcase
 end

 
//DECODER SECTION OF STATE MACHINE
 always @(*)			  							// logic to drive MEALY FSM outputs (FSM DECODER SECTION)
 begin
	if (current_state == INITIALIZE1)		// force outside counters to be initialized to zero for first state
			begin
				init			= 1'b1;
				x_count_en 	= 1'b0;
				x_up1_dwn0  = 1'b0;				
				y_count_en 	= 1'b0;
				y_up1_dwn0  = 1'b0;				
				capt_enbl 	= 1'b0;
			end	

	else if (current_state == INITIALIZE2) // keep outside counters initialized to zero for another state
			begin
				init			= 1'b0;
				x_count_en 	= 1'b0;
				x_up1_dwn0  = 1'b0;				
				y_count_en 	= 1'b0;
				y_up1_dwn0  = 1'b0;				
				capt_enbl 	= 1'b0;
			end	

	else if (current_state == CAPTURE_XY) // motion button has been pressed. activate outside x/y register capture enables
			begin
				init			= 1'b0;
				x_count_en 	= 1'b0;
				x_up1_dwn0  = 1'b0;				
				y_count_en 	= 1'b0; 
				y_up1_dwn0  = 1'b0;				
				capt_enbl 	= 1'b1;
			end	
	else if (current_state == IN_MOTION) // motion input button release. x/Y motion is now enabled
			begin
				init			= 1'b0;
				x_count_en 	= ((x_comp_gt) | (x_comp_lt));
				x_up1_dwn0  = x_comp_lt;				
				y_count_en 	= ((y_comp_gt) | (y_comp_lt));
				y_up1_dwn0  = y_comp_lt;
				capt_enbl 	= 1'b0;
			end
	else											// default case (used in AT_REST and XY_REACHED states), is to deactivate all FSM outputs.
			begin
				init			= 1'b0;
				x_count_en 	= 1'b0;
				x_up1_dwn0  = 1'b0;				
				y_count_en 	= 1'b0;
				y_up1_dwn0  = 1'b0;				
				capt_enbl 	= 1'b0;
			end
end
endmodule
