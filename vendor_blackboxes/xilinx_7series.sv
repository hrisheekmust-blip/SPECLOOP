// Xilinx 7-series and UltraScale common primitives — blackbox stubs
// SpecLoop uses these when a design references Xilinx primitives without
// providing source.  Yosys honours the (* blackbox *) attribute.

(* blackbox *)
module IBUF (output O, input I);
endmodule

(* blackbox *)
module OBUF (output O, input I);
endmodule

(* blackbox *)
module BUFG (output O, input I);
endmodule

(* blackbox *)
module BUFR #(parameter BUFR_DIVIDE = "BYPASS", parameter SIM_DEVICE = "7SERIES")
    (output O, input I, input CE, input CLR);
endmodule

(* blackbox *)
module FDRE #(parameter INIT = 1'b0, parameter IS_C_INVERTED = 1'b0,
              parameter IS_D_INVERTED = 1'b0, parameter IS_R_INVERTED = 1'b0)
    (output Q, input C, input CE, input D, input R);
endmodule

(* blackbox *)
module FDSE #(parameter INIT = 1'b1)
    (output Q, input C, input CE, input D, input S);
endmodule

(* blackbox *)
module FDCE #(parameter INIT = 1'b0)
    (output Q, input C, input CE, input D, input CLR);
endmodule

(* blackbox *)
module FDPE #(parameter INIT = 1'b1)
    (output Q, input C, input CE, input D, input PRE);
endmodule

(* blackbox *)
module LUT1 #(parameter INIT = 2'h0) (output O, input I0);
endmodule

(* blackbox *)
module LUT2 #(parameter INIT = 4'h0) (output O, input I0, input I1);
endmodule

(* blackbox *)
module LUT3 #(parameter INIT = 8'h0) (output O, input I0, input I1, input I2);
endmodule

(* blackbox *)
module LUT4 #(parameter INIT = 16'h0) (output O, input I0, input I1, input I2, input I3);
endmodule

(* blackbox *)
module LUT5 #(parameter INIT = 32'h0) (output O, input I0, input I1, input I2, input I3, input I4);
endmodule

(* blackbox *)
module LUT6 #(parameter INIT = 64'h0) (output O, input I0, input I1, input I2, input I3, input I4, input I5);
endmodule

(* blackbox *)
module CARRY4 (
    output [3:0] O, output CO,
    input  [3:0] DI, input [3:0] S, input CI, input CYINIT
);
endmodule

(* blackbox *)
module DSP48E2 #(
    parameter ACASCREG = 1, parameter ADREG = 1, parameter ALUMODEREG = 1,
    parameter AREG = 1, parameter BCASCREG = 1, parameter BREG = 1,
    parameter CARRYINREG = 1, parameter CARRYINSELREG = 1, parameter CREG = 1,
    parameter DREG = 1, parameter INMODEREG = 1, parameter MREG = 1,
    parameter OPMODEREG = 1, parameter PREG = 1,
    parameter A_INPUT = "DIRECT", parameter B_INPUT = "DIRECT",
    parameter [47:0] MASK = 48'h3FFFFFFFFFFF,
    parameter [47:0] PATTERN = 48'h000000000000,
    parameter SEL_MASK = "MASK", parameter SEL_PATTERN = "PATTERN",
    parameter USE_MULT = "MULTIPLY", parameter USE_PATTERN_DETECT = "NO_PATDET",
    parameter USE_SIMD = "ONE48", parameter USE_WIDEXOR = "FALSE",
    parameter XORSIMD = "XOR24_48_96"
) (
    output [29:0] ACOUT, output [17:0] BCOUT, output CARRYCASCOUT,
    output [3:0] CARRYOUT, output MULTSIGNOUT, output OVERFLOW,
    output [47:0] P, output PATTERNBDETECT, output PATTERNDETECT,
    output [47:0] PCOUT, output UNDERFLOW, output [7:0] XOROUT,
    input [29:0] A, input [17:0] B, input [47:0] C, input [26:0] D,
    input [29:0] ACIN, input [17:0] BCIN, input CARRYCASCIN, input CARRYIN,
    input [3:0] CARRYINSEL, input CEA1, input CEA2, input CEAD, input CEALUMODE,
    input CEB1, input CEB2, input CEC, input CECARRYIN, input CECTRL,
    input CED, input CEINMODE, input CEM, input CEP, input CLK,
    input [3:0] INMODE, input MULTSIGNIN, input [6:0] OPMODE,
    input [47:0] PCIN, input RSTA, input RSTALLCARRYIN, input RSTALUMODE,
    input RSTB, input RSTC, input RSTCTRL, input RSTD, input RSTINMODE,
    input RSTM, input RSTP
);
endmodule

(* blackbox *)
module RAMB36E2 #(
    parameter CASCADE_ORDER_A = "NONE", parameter CASCADE_ORDER_B = "NONE",
    parameter CLOCK_DOMAINS = "INDEPENDENT",
    parameter DOA_REG = 1, parameter DOB_REG = 1,
    parameter ENADDRENA = "FALSE", parameter ENADDRENB = "FALSE",
    parameter EN_ECC_PIPE = "FALSE", parameter EN_ECC_READ = "FALSE",
    parameter EN_ECC_WRITE = "FALSE",
    parameter READ_WIDTH_A = 0, parameter READ_WIDTH_B = 0,
    parameter WRITE_MODE_A = "NO_CHANGE", parameter WRITE_MODE_B = "NO_CHANGE",
    parameter WRITE_WIDTH_A = 0, parameter WRITE_WIDTH_B = 0
) (
    output [31:0] DOUTADOUT, output [3:0] DOUTPADOUTP,
    output [31:0] DOUTBDOUT, output [3:0] DOUTPBDOUTP,
    output ECCPARITY, output RDADDRECC,
    input ADDRARDADDR, input ADDRBWRADDR,
    input CLKARDCLK, input CLKBWRCLK,
    input DINADIN, input DINPADINP, input DINBDIN, input DINPBDINP,
    input ENARDEN, input ENBWREN, input INJECTDBITERR, input INJECTSBITERR,
    input REGCEAREGCE, input REGCEB, input RSTRAMARSTRAM, input RSTRAMB,
    input RSTREGARSTREG, input RSTREGB, input SLEEP,
    input [3:0] WEA, input [7:0] WEBWE
);
endmodule
