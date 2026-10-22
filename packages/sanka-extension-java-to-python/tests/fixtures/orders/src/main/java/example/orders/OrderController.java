// SPDX-License-Identifier: Apache-2.0
package example.orders;
import java.util.List;
import jakarta.validation.Valid;
import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.transaction.annotation.Transactional;
import org.springframework.web.bind.annotation.RestController;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.PutMapping;
import org.springframework.web.bind.annotation.DeleteMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.PathVariable;
@RestController
@RequestMapping("/orders")
public class OrderController {
    private final OrderRepository repository;
    public OrderController(OrderRepository repository) { this.repository = repository; }
    @GetMapping("/all")
    public List<Order> list() { return repository.findAllByOrderByIdAsc(); }
    @GetMapping("/{id}")
    public Order get(@PathVariable("id") long id) {
        return repository.findById(id).orElseThrow(NotFound::new);
    }
    @PostMapping("/new")
    @Transactional
    public ResponseEntity<Order> create(@Valid @RequestBody OrderInput input) {
        Order entity = new Order();
        entity.name = input.name();
        entity.amount = input.amount();
        entity.note = input.note();
        return ResponseEntity.status(HttpStatus.CREATED).body(repository.saveAndFlush(entity));
    }
    @PutMapping("/{id}")
    @Transactional
    public ResponseEntity<Order> update(@PathVariable("id") long id, @Valid @RequestBody OrderInput input) {
        Order entity = repository.findById(id).orElseThrow(NotFound::new);
        entity.name = input.name();
        entity.amount = input.amount();
        entity.note = input.note();
        return ResponseEntity.ok(repository.saveAndFlush(entity));
    }
    @DeleteMapping("/{id}")
    @Transactional
    public ResponseEntity<Void> delete(@PathVariable("id") long id) {
        Order entity = repository.findById(id).orElseThrow(NotFound::new);
        repository.delete(entity);
        return ResponseEntity.noContent().build();
    }
    @PostMapping("/rollback")
    @Transactional
    public Order rollback(@Valid @RequestBody OrderInput input) {
        Order entity = new Order();
        entity.name = input.name();
        entity.amount = input.amount();
        entity.note = input.note();
        repository.saveAndFlush(entity);
        throw new RejectedWrite();
    }
}
