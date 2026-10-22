// SPDX-License-Identifier: Apache-2.0
package example.greetings;
public record Greeting(String message, long requestId, boolean active, String note) {}
