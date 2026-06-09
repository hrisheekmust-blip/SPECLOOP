module counter_spec (
  input logic clk,
  input logic rst_n,
  input logic en,
  input logic [7:0] count
);

  // Decode wires for readability
  wire at_max = (count == 8'hFF);
  wire past_at_max = (($past(count) == 8'hFF));
  wire count_incremented = (count == $past(count) + 8'h01);
  wire count_wrapped = (count == 8'h00 && $past(count) == 8'hFF);
  wire count_stable = (count == $past(count));

  // Reset assertions
  always @(posedge clk) begin
    // Property 1: reset_clears_count_async
    if (!rst_n) begin
      ap_reset_clears_count_async: assert(count == 8'h00);
    end

    // Property 2: reset_to_zero_on_release
    if ($past(1'b1) && $rose(rst_n)) begin
      ap_reset_to_zero_on_release: assert(count == 8'h00);
    end

    // Property 11: reset_dominates_enable
    if (!rst_n && en) begin
      ap_reset_dominates_enable: assert(count == 8'h00);
    end
  end

  // Functional assertions
  always @(posedge clk) begin
    if ($past(1'b1) && rst_n && $past(rst_n)) begin
      // Property 3: count_increments_when_enabled
      if ($past(en) && !past_at_max) begin
        ap_count_increments_when_enabled: assert(count_incremented);
      end

      // Property 4: count_holds_when_disabled
      if (!$past(en)) begin
        ap_count_holds_when_disabled: assert(count_stable);
      end

      // Property 5: count_wraps_at_max
      if ($past(en) && past_at_max) begin
        ap_count_wraps_at_max: assert(count == 8'h00);
      end

      // Property 7: enable_causes_increment_next_cycle
      if ($rose(en) && !past_at_max) begin
        ap_enable_causes_increment_next_cycle: assert(count_incremented);
      end

      // Property 8: disable_freezes_next_cycle
      if ($fell(en)) begin
        ap_disable_freezes_next_cycle: assert(count_stable);
      end

      // Property 9: count_only_changes_on_clock
      if ($past(en)) begin
        ap_count_only_changes_on_clock: assert(count_incremented || count_wrapped);
      end

      // Property 10: no_glitches_during_normal_operation
      if ($past(en, 2) && $past(en) && $past(rst_n, 2)) begin
        ap_no_glitches_during_normal_operation: assert(count_incremented || count_wrapped || count_stable);
      end
    end
  end

  // Safety assertions (combinational, checked every cycle)
  always @(posedge clk) begin
    // Property 6: count_in_valid_range
    ap_count_in_valid_range: assert(count >= 8'h00 && count <= 8'hFF);
  end

  // Temporal multi-cycle assertions
  always @(posedge clk) begin
    if ($past(1'b1, 2) && rst_n && $past(rst_n) && $past(rst_n, 2)) begin
      // Property 12: count_stable_across_multiple_disabled_cycles
      if (!$past(en, 2) && !$past(en)) begin
        ap_count_stable_across_multiple_disabled_cycles: assert($past(count, 2) == count);
      end
    end
  end

endmodule

bind counter counter_spec spec_inst(.*);