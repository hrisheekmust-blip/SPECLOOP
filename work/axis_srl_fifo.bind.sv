module axis_srl_fifo_spec #(
  parameter DATA_WIDTH = 8,
  parameter KEEP_ENABLE = 1'b0,
  parameter KEEP_WIDTH = 1,
  parameter LAST_ENABLE = 1,
  parameter ID_ENABLE = 0,
  parameter ID_WIDTH = 8,
  parameter DEST_ENABLE = 0,
  parameter DEST_WIDTH = 8,
  parameter USER_ENABLE = 1,
  parameter USER_WIDTH = 1,
  parameter DEPTH = 16
)(
  input logic clk,
  input logic rst,
  input logic [DATA_WIDTH-1:0] s_axis_tdata,
  input logic [KEEP_WIDTH-1:0] s_axis_tkeep,
  input logic s_axis_tvalid,
  input logic s_axis_tready,
  input logic s_axis_tlast,
  input logic [ID_WIDTH-1:0] s_axis_tid,
  input logic [DEST_WIDTH-1:0] s_axis_tdest,
  input logic [USER_WIDTH-1:0] s_axis_tuser,
  input logic [DATA_WIDTH-1:0] m_axis_tdata,
  input logic [KEEP_WIDTH-1:0] m_axis_tkeep,
  input logic m_axis_tvalid,
  input logic m_axis_tready,
  input logic m_axis_tlast,
  input logic [ID_WIDTH-1:0] m_axis_tid,
  input logic [DEST_WIDTH-1:0] m_axis_tdest,
  input logic [USER_WIDTH-1:0] m_axis_tuser,
  input logic [4:0] count
);

  wire empty_reg;
  wire full_reg;
  wire [4:0] ptr_reg;

  assign empty_reg = axis_srl_fifo.empty_reg;
  assign full_reg = axis_srl_fifo.full_reg;
  assign ptr_reg = axis_srl_fifo.ptr_reg;

  wire write_xfer;
  wire read_xfer;
  wire simultaneous_xfer;

  assign write_xfer = s_axis_tvalid && s_axis_tready;
  assign read_xfer = m_axis_tvalid && m_axis_tready;
  assign simultaneous_xfer = write_xfer && read_xfer;

  always @(posedge clk) begin
    if (rst) begin
      ap_reset_empty: assert(empty_reg == 1'b1);
      ap_reset_full: assert(full_reg == 1'b0);
      ap_reset_ptr: assert(ptr_reg == 0);
    end

    if (!rst) begin
      ap_slave_ready_when_not_full: assert(!full_reg || s_axis_tready);
      ap_master_valid_when_not_empty: assert(!empty_reg || m_axis_tvalid);
      ap_no_ready_when_full: assert(full_reg || !s_axis_tready);
      ap_no_valid_when_empty: assert(empty_reg || !m_axis_tvalid);
      ap_count_equals_ptr: assert(count == ptr_reg);
      ap_full_flag_consistency: assert(full_reg == (ptr_reg == DEPTH));
      ap_empty_flag_consistency: assert(empty_reg == (ptr_reg == 0));
      ap_ptr_never_exceeds_depth: assert(ptr_reg <= DEPTH);
      ap_no_write_when_full: assert(full_reg || !write_xfer);
      ap_no_read_when_empty: assert(empty_reg || !read_xfer);
    end

    if ($past(1'b1) && !rst && !$past(rst)) begin
      if ($past(write_xfer && !read_xfer)) begin
        ap_ptr_increments_on_write_only: assert(ptr_reg == $past(ptr_reg) + 1);
      end

      if ($past(read_xfer && !write_xfer)) begin
        ap_ptr_decrements_on_read_only: assert(ptr_reg == $past(ptr_reg) - 1);
      end

      if ($past(simultaneous_xfer)) begin
        ap_ptr_stable_on_simultaneous_read_write: assert(ptr_reg == $past(ptr_reg));
      end

      if ($past(empty_reg && write_xfer && !read_xfer)) begin
        ap_empty_to_nonempty_transition: assert(!empty_reg);
      end

      if ($past(full_reg && read_xfer && !write_xfer)) begin
        ap_full_to_nonfull_transition: assert(!full_reg);
      end

      if ($past(empty_reg && write_xfer)) begin
        ap_data_integrity_single_entry: assert(m_axis_tdata == $past(s_axis_tdata));
      end
    end
  end

endmodule

bind axis_srl_fifo axis_srl_fifo_spec #(
  .DATA_WIDTH(DATA_WIDTH),
  .KEEP_ENABLE(KEEP_ENABLE),
  .KEEP_WIDTH(KEEP_WIDTH),
  .LAST_ENABLE(LAST_ENABLE),
  .ID_ENABLE(ID_ENABLE),
  .ID_WIDTH(ID_WIDTH),
  .DEST_ENABLE(DEST_ENABLE),
  .DEST_WIDTH(DEST_WIDTH),
  .USER_ENABLE(USER_ENABLE),
  .USER_WIDTH(USER_WIDTH),
  .DEPTH(DEPTH)
) spec_inst(.*);
