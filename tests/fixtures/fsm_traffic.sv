module fsm_traffic (
    input  logic clk,
    input  logic rst_n,
    input  logic timer_done,
    output logic red,
    output logic yellow,
    output logic green
);
    typedef enum logic [1:0] {
        RED    = 2'b00,
        GREEN  = 2'b01,
        YELLOW = 2'b10
    } state_t;

    state_t state, next_state;

    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) state <= RED;
        else        state <= next_state;
    end

    always_comb begin
        next_state = state;
        case (state)
            RED:    if (timer_done) next_state = GREEN;
            GREEN:  if (timer_done) next_state = YELLOW;
            YELLOW: if (timer_done) next_state = RED;
            default: next_state = RED;
        endcase
    end

    assign red    = (state == RED);
    assign green  = (state == GREEN);
    assign yellow = (state == YELLOW);
endmodule
