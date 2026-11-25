# Host test of the spectrum signal chain.
# Written from SPEC-LED.md and public standards (W3C Web Audio API AnalyserNode maths); no third-party code.
#
#   make -C test/host                                      # g++ (C++11)
#   make -C test/host CXX=gcc LANGOPT="-x c -std=c99"      # a plain C compiler: the sources are C99 / C++ common subset
#
# Targets: all (= run), run (build + run), check (reference header up to date, then run), clean.

CXX ?= g++
LANGOPT ?= -x c++ -std=c++11
OPT ?= -O2
PYTHON ?= python3
WARN = -Wall -Wextra -pedantic
INC = -I../../include

TARGET = spectrum_host_test
SOURCES = spectrum_host_test.cpp ../../src/spectrum_dsp.cpp
DEPS = spectrum_expected.h ../../include/spectrum_dsp.h

.PHONY: all run check clean

all: run

$(TARGET): $(SOURCES) $(DEPS)
	$(CXX) $(LANGOPT) $(WARN) $(OPT) $(INC) $(SOURCES) -o $@ -lm

run: $(TARGET)
	./$(TARGET)

check:
	$(PYTHON) -I -B ../../tools/tests/spectrum_reference.py --check
	$(MAKE) run

clean:
	rm -f $(TARGET) $(TARGET).exe
