# Math in Lime

Selectable text stays text. Inline math such as $E=mc^2$ and
$\frac{a}{b}$ shares a baseline with the surrounding words.

## Fourier optics

$$
\widehat{f}(\xi)=\int_{-\infty}^{\infty}f(x)e^{-2\pi i x\xi}\,\mathrm{d}x\tag{1}
$$

```typst-math
U(x,z) = frac(e^(i k z), sqrt(i lambda z)) integral_(-oo)^oo U_0(x') e^(frac(i pi (x-x')^2,lambda z)) thin upright(d) x'
```

Both LaTeX and native Typst equations use the terminal foreground color.

## Math inside containers

- A short expression $\alpha+\beta$ stays with the text.
- A fraction $\frac{1}{1+x^2}$ reserves enough rows to avoid neighbouring text.
  - Nested lists work too: $\sqrt{x^2+y^2}$.

> A quote with $\int_0^1 x\,\mathrm{d}x=\frac12$ and real, selectable words.
>
> $$
> f(x)=\begin{cases}x^2 & x\geq 0\\-x & x<0\end{cases}
> $$

## Matrices, alignment and macros

$$
\begin{pmatrix}1&0\\0&e^{i\phi}\end{pmatrix}\begin{pmatrix}a\\b\end{pmatrix}
$$

$$
\begin{aligned}(a+b)^2 &= a^2+2ab+b^2 \\ &= a(a+b)+b(a+b)\end{aligned}
$$

$$
\newcommand{\vect}[1]{\boldsymbol{#1}}\vect{k}\cdot\vect{x}
$$

## Source and fallback

Run with --show-math-source to keep equation source searchable and copyable.
Code stays code: `$x^2$`, escaped dollars stay literal: \$5, and prices such as
$5 and $10 are ordinary text.

An unsupported command retains its source:

$$
\thisCommandDoesNotExist{x}
$$

Math in tables, headings and links remains readable source.

| Description | Source |
|---|---|
| Energy | $E=mc^2$ |
