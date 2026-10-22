// SPDX-License-Identifier: Apache-2.0
package example.orders;
import java.math.BigDecimal;
import jakarta.persistence.Entity;
import jakarta.persistence.Table;
import jakarta.persistence.Id;
import jakarta.persistence.GeneratedValue;
import jakarta.persistence.GenerationType;
import jakarta.persistence.Column;
import com.fasterxml.jackson.annotation.JsonFormat;
@Entity
@Table(name="orders")
public class Order {
    @Id
    @GeneratedValue(strategy=GenerationType.IDENTITY)
    @Column(name="id", nullable=false)
    public Long id;
    @Column(name="name", nullable=false, length=80)
    public String name;
    @Column(name="amount", nullable=false, precision=14, scale=2)
    @JsonFormat(shape=JsonFormat.Shape.STRING)
    public BigDecimal amount;
    @Column(name="note", length=200)
    public String note;
}
