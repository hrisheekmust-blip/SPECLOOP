// Intel/Altera Stratix and Cyclone common primitives — blackbox stubs

(* blackbox *)
module lcell (output cout, input in);
endmodule

(* blackbox *)
module dff (output q, input d, input clk, input clrn, input prn, input ena);
endmodule

(* blackbox *)
module dffea (output q, input d, input clk, input clrn, input prn, input ena, input adata);
endmodule

(* blackbox *)
module altsyncram #(
    parameter address_aclr_a         = "UNUSED",
    parameter address_aclr_b         = "UNUSED",
    parameter address_reg_b          = "CLOCK1",
    parameter byte_size              = 8,
    parameter clock_enable_core_a    = "USE_INPUT_CLKEN",
    parameter clock_enable_core_b    = "USE_INPUT_CLKEN",
    parameter clock_enable_input_a   = "BYPASS",
    parameter clock_enable_input_b   = "BYPASS",
    parameter clock_enable_output_a  = "BYPASS",
    parameter clock_enable_output_b  = "BYPASS",
    parameter intended_device_family = "Stratix IV",
    parameter lpm_type               = "altsyncram",
    parameter numwords_a             = 256,
    parameter numwords_b             = 256,
    parameter operation_mode         = "BIDIR_DUAL_PORT",
    parameter outdata_aclr_a         = "NONE",
    parameter outdata_aclr_b         = "NONE",
    parameter outdata_reg_a          = "UNREGISTERED",
    parameter outdata_reg_b          = "UNREGISTERED",
    parameter power_up_uninitialized = "FALSE",
    parameter read_during_write_mode_mixed_ports = "DONT_CARE",
    parameter read_during_write_mode_port_a      = "NEW_DATA_NO_NBE_READ",
    parameter read_during_write_mode_port_b      = "NEW_DATA_NO_NBE_READ",
    parameter width_a                = 8,
    parameter width_b                = 8,
    parameter width_byteena_a        = 1,
    parameter width_byteena_b        = 1,
    parameter widthad_a              = 8,
    parameter widthad_b              = 8
) (
    output [width_a-1:0] q_a,
    output [width_b-1:0] q_b,
    input  [widthad_a-1:0] address_a,
    input  [widthad_b-1:0] address_b,
    input  clock0, input clock1,
    input  clocken0, input clocken1, input clocken2, input clocken3,
    input  aclr0, input aclr1,
    input  [width_a-1:0] data_a,
    input  [width_b-1:0] data_b,
    input  [width_byteena_a-1:0] byteena_a,
    input  [width_byteena_b-1:0] byteena_b,
    input  wren_a, input wren_b,
    input  rden_a, input rden_b,
    input  eccstatus
);
endmodule

(* blackbox *)
module alt_inbuf #(parameter IO_STANDARD = "SSTL-15") (output o, input i);
endmodule

(* blackbox *)
module alt_outbuf #(parameter IO_STANDARD = "SSTL-15") (output o, input i);
endmodule

(* blackbox *)
module alt_iobuf #(parameter IO_STANDARD = "SSTL-15") (inout io, input i, input oe, output o);
endmodule

(* blackbox *)
module altpll #(
    parameter bandwidth_type        = "AUTO",
    parameter clk0_divide_by        = 1,
    parameter clk0_duty_cycle       = 50,
    parameter clk0_multiply_by      = 1,
    parameter clk0_phase_shift      = "0",
    parameter compensate_clock      = "CLK0",
    parameter inclk0_input_frequency = 10000,
    parameter intended_device_family = "Cyclone IV E",
    parameter lpm_type              = "altpll",
    parameter operation_mode        = "NORMAL"
) (
    output [4:0] clk,
    output locked,
    input  [1:0] inclk,
    input  activeclock, input areset, input clkena,
    input  clkswitch, input configupdate, input enable0, input enable1,
    input  extclkena, input fbin, input pfdena, input phasecounterselect,
    input  phasedone, input phasestep, input phaseupdown, input pllena,
    input  scanaclr, input scanclk, input scanclkena, input scandata,
    output scandataout, output scandone, output sclkout0, output sclkout1,
    input  scanread, input scanwrite, input phasecounterselect_in
);
endmodule
