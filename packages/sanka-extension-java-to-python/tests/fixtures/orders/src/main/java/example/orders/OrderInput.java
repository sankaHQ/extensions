// SPDX-License-Identifier: Apache-2.0
package example.orders;
import java.math.BigDecimal;
import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.NotNull;
import jakarta.validation.constraints.Size;
import jakarta.validation.constraints.DecimalMin;
import jakarta.validation.constraints.Digits;
public record OrderInput(
    @NotBlank @Size(max=80) String name,
    @NotNull @DecimalMin("0.00") @Digits(integer=12, fraction=2) BigDecimal amount,
    @Size(max=200) String note
) {}
