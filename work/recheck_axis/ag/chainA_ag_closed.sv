// ===== Assume-Guarantee contract-stubs for Chain A =====
// Each stub: same interface, FREE (anyseq) outputs CONSTRAINED only by that block's
// PROVEN guarantees (cited). No RTL internals -> scalable, FIFO DEPTH irrelevant.

module reg_stub(input clk,input rst,
 input [7:0] s_d,input s_v,output s_r,input s_l,input s_u,
 output [7:0] m_d,output m_v,input m_r,output m_l,output m_u);
 (* anyseq *) reg sr; (* anyseq *) reg [7:0] md; (* anyseq *) reg mv,ml,mu;
 assign s_r=sr; assign m_d=md; assign m_v=mv; assign m_l=ml; assign m_u=mu;
 always @(posedge clk) if (rst) begin
   assume(!m_v);   // G1 = ap_reset_master_valid
   assume(!s_r);   // G2 = ap_reset_slave_ready
 end
 always @(posedge clk) if (!rst && $past(!rst) && $past(m_v) && !$past(m_r)) begin
   assume(m_d==$past(m_d)); assume(m_l==$past(m_l)); assume(m_u==$past(m_u)); // G4 = ap_master_data_stable_t{data,last,user}
 end
endmodule

module fifo_stub(input clk,input rst,
 input [7:0] s_d,input s_v,output s_r,input s_l,input s_u,
 output [7:0] m_d,output m_v,input m_r,output m_l,output m_u);
 (* anyseq *) reg sr; (* anyseq *) reg [7:0] md; (* anyseq *) reg mv,ml,mu;
 assign s_r=sr; assign m_d=md; assign m_v=mv; assign m_l=ml; assign m_u=mu;
 // CONTRACT ADDED (now proven — inline fallback: axis_fifo m_axis data-stable = PASS):
 always @(posedge clk) if (!rst && $past(!rst) && $past(m_v) && !$past(m_r)) assume(m_d==$past(m_d));
endmodule

module fla_stub(input clk,input rst,
 input [7:0] s_d,input s_v,output s_r,input s_l,input s_u,
 output [7:0] m_d,output m_v,input m_r,output m_l,output m_u);
 (* anyseq *) reg sr; (* anyseq *) reg [7:0] md; (* anyseq *) reg mv,ml,mu;
 assign s_r=sr; assign m_d=md; assign m_v=mv; assign m_l=ml; assign m_u=mu;
 always @(posedge clk) if (rst) assume(!s_r);   // G2 = ap_reset_ready  (NO proven G1: reset->m_valid=0)
 always @(posedge clk) if (!rst && $past(!rst) && $past(m_v) && !$past(m_r))
   assume(m_d==$past(m_d));                      // G4 = ap_master_data_stable
endmodule

module rl_stub(input clk,input rst,
 input [7:0] s_d,input s_v,output s_r,input s_l,input s_u,
 output [7:0] m_d,output m_v,input m_r,output m_l,output m_u);
 (* anyseq *) reg sr; (* anyseq *) reg [7:0] md; (* anyseq *) reg mv,ml,mu;
 assign s_r=sr; assign m_d=md; assign m_v=mv; assign m_l=ml; assign m_u=mu;
 always @(posedge clk) if (rst) begin
   assume(!m_v);  // G1 = ap_reset_m_valid
   assume(!s_r);  // G2 = ap_reset_s_ready
 end
 always @(posedge clk) if (!rst && $past(!rst) && $past(m_v) && !$past(m_r))
   assume(m_d==$past(m_d));  // G4 = ap_master_data_stable_tdata / ap_output_reg_stable_tdata
 // CONTRACT ADDED (now proven — inline fallback: axis_rate_limit valid-held = PASS; the stored
 // ap_master_valid_stable was mis-formulated, the correct property holds):
 always @(posedge clk) if (!rst && $past(!rst) && $past(m_v) && !$past(m_r)) assume(m_v);
endmodule

module chainA_ag(input clk,
 input [7:0] in_d,input in_v,output in_r,input in_l,input in_u,
 output [7:0] out_d,output out_v,input out_r,output out_l,output out_u);
 reg [3:0] __rc=0; always @(posedge clk) if(__rc<15) __rc<=__rc+1;
 wire rst = (__rc < 3);   // driven reset: $past well-defined after release
 wire [7:0] b1d,b2d,b3d; wire b1v,b1r,b1l,b1u, b2v,b2r,b2l,b2u, b3v,b3r,b3l,b3u;
 reg_stub  u1(clk,rst, in_d,in_v,in_r,in_l,in_u,  b1d,b1v,b1r,b1l,b1u);
 fifo_stub u2(clk,rst, b1d,b1v,b1r,b1l,b1u,        b2d,b2v,b2r,b2l,b2u);
 fla_stub  u3(clk,rst, b2d,b2v,b2r,b2l,b2u,        b3d,b3v,b3r,b3l,b3u);
 rl_stub   u4(clk,rst, b3d,b3v,b3r,b3l,b3u,        out_d,out_v,out_r,out_l,out_u);

 // external A_head: a well-behaved AXIS source drives the composition input
 always @(posedge clk) if (rst) assume(!in_v);
 always @(posedge clk) if (!rst && $past(in_v) && !$past(in_r)) begin
   assume(in_v); assume(in_d==$past(in_d)); assume(in_l==$past(in_l)); assume(in_u==$past(in_u));
 end

 // ===== INTERACTION PROPERTIES =====
 always @(posedge clk) if (rst) begin
   ap_reset_out_valid: assert(!out_v);     // reset cleanliness (composition output)
   ap_reset_in_ready:  assert(!in_r);      // reset cleanliness (composition input ready)
 end
 // backpressure DATA stability per boundary (the FIXED property: producer holds stalled beat)
 always @(posedge clk) if (!rst && $past(!rst) && $past(b1v)  && !$past(b1r))  ap_bp1_data: assert(b1d==$past(b1d));        // reg -> fifo
 always @(posedge clk) if (!rst && $past(!rst) && $past(b2v)  && !$past(b2r))  ap_bp2_data: assert(b2d==$past(b2d));        // fifo -> fla
 always @(posedge clk) if (!rst && $past(!rst) && $past(b3v)  && !$past(b3r))  ap_bp3_data: assert(b3d==$past(b3d));        // fla -> rl
 always @(posedge clk) if (!rst && $past(!rst) && $past(out_v)&& !$past(out_r))ap_bpout_data:assert(out_d==$past(out_d));    // rl -> output
 // backpressure VALID stability (needs G3, which NO block proves)
 always @(posedge clk) if (!rst && $past(!rst) && $past(out_v)&& !$past(out_r))ap_bpout_valid:assert(out_v);
endmodule
