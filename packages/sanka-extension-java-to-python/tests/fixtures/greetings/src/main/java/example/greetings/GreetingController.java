// SPDX-License-Identifier: Apache-2.0
package example.greetings;
import java.util.Map;
import org.springframework.web.bind.annotation.RestController;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.PathVariable;
@RestController
@RequestMapping("/api")
public class GreetingController {
    @GetMapping("/status")
    public Map<String, Object> status() { return Map.of("service", "greetings", "ok", true, "large", 9007199254740993L); }
    @GetMapping("/greetings")
    public Greeting greet(@RequestParam(name="name", defaultValue="world") String name) {
        return new Greeting("Hello, " + name, 9007199254740993L, true, null);
    }
    @GetMapping("/greetings/{name}")
    public Greeting named(@PathVariable("name") String name) {
        return new Greeting("Hello, " + name, 9007199254740993L, true, null);
    }
}
