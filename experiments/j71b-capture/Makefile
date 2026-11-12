CC ?= cc
CFLAGS ?= -O2 -g
WARN = -std=c11 -Wall -Wextra -Werror -pedantic
BUILD = build

.PHONY: all test sanitize clean
all: $(BUILD)/j71b-capture.so

$(BUILD):
	mkdir -p $@

$(BUILD)/j71b-capture.so: capture.c | $(BUILD)
	$(CC) $(CPPFLAGS) $(CFLAGS) $(WARN) -fPIC -shared $< -lm -o $@

$(BUILD)/test-capture: test_capture.c capture.c | $(BUILD)
	$(CC) $(CPPFLAGS) $(CFLAGS) $(WARN) $< -lm -o $@

test: all $(BUILD)/test-capture
	$(BUILD)/test-capture
	python3 -m unittest -v test_graph test_hardware test_session_helpers

sanitize: | $(BUILD)
	$(CC) $(CPPFLAGS) -g -O1 $(WARN) -fsanitize=address,undefined \
		-fno-omit-frame-pointer test_capture.c -lm -o $(BUILD)/test-sanitize
	ASAN_OPTIONS=detect_leaks=1 $(BUILD)/test-sanitize

clean:
	rm -rf $(BUILD) __pycache__
