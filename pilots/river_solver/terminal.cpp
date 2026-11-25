// Exact card removal using prefix sums for the 16 subsets of four cards.
// Compile this optional pilot kernel with cl /O2 /LD terminal.cpp on Windows,
// or c++ -O3 -shared -fPIC terminal.cpp -o terminal.so on Linux.
#ifdef _WIN32
#define EXPORT __declspec(dllexport)
#else
#define EXPORT
#endif
extern "C" EXPORT void terminal_product(const int* order, int positions,
    const int* offsets, const int* terms, int hands, const double* reach,
    int showdown, double* prefix, double* output) {
    double sum = 0;
    for (int i=0; i<positions; ++i) {
        sum = order[i] < 0 ? 0 : sum + reach[order[i]];
        prefix[i] = sum;
    }
    for (int h=0; h<hands; ++h) {
        double value = 0;
        for (int j=offsets[h]; j<offsets[h+1]; ++j) {
            const int* t = terms+4*j;
            double mass = prefix[t[2]];
            if (showdown) mass = prefix[t[0]] + prefix[t[1]] - mass;
            value += t[3]*mass;
        }
        output[h] = value;
    }
}
