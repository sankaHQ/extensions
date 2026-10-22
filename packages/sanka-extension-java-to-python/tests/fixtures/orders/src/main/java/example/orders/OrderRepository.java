// SPDX-License-Identifier: Apache-2.0
package example.orders;
import java.util.List;
import org.springframework.data.jpa.repository.JpaRepository;
public interface OrderRepository extends JpaRepository<Order, Long> {
    List<Order> findAllByOrderByIdAsc();
}
