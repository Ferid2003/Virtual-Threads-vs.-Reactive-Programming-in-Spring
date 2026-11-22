package bench.stub;

import java.time.Duration;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.RestController;
import reactor.core.publisher.Mono;

// Simulated downstream: non-blocking timer delay, so the stub never runs out of threads at high rates.
@SpringBootApplication
@RestController
public class StubApplication {

    public static void main(String[] args) {
        SpringApplication.run(StubApplication.class, args);
    }

    record Customer(long id, String name, String tier) {}

    private static final String[] TIERS = {"BRONZE", "SILVER", "GOLD"};

    private final Duration delay;

    StubApplication(@Value("${stub.delay-ms}") long delayMs) {
        this.delay = Duration.ofMillis(delayMs);
    }

    @GetMapping("/customers/{id}")
    Mono<Customer> customer(@PathVariable long id) {
        return Mono.delay(delay).thenReturn(new Customer(id, "customer-" + id, TIERS[(int) (id % TIERS.length)]));
    }
}
