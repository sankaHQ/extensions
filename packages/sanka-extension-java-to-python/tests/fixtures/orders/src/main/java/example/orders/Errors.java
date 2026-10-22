// SPDX-License-Identifier: Apache-2.0
package example.orders;
import org.springframework.web.bind.annotation.RestControllerAdvice;
import org.springframework.web.bind.annotation.ExceptionHandler;
import org.springframework.http.ResponseEntity;
import org.springframework.http.HttpStatus;
import org.springframework.web.bind.MethodArgumentNotValidException;
import org.springframework.http.converter.HttpMessageNotReadableException;
import org.springframework.web.method.annotation.MethodArgumentTypeMismatchException;
@RestControllerAdvice
public class Errors {
    @ExceptionHandler(MethodArgumentNotValidException.class)
    public ResponseEntity<ApiError> invalid() { return ResponseEntity.status(HttpStatus.BAD_REQUEST).body(new ApiError("invalid_request")); }
    @ExceptionHandler(HttpMessageNotReadableException.class)
    public ResponseEntity<ApiError> unreadable() { return ResponseEntity.status(HttpStatus.BAD_REQUEST).body(new ApiError("invalid_request")); }
    @ExceptionHandler(MethodArgumentTypeMismatchException.class)
    public ResponseEntity<ApiError> parameter() { return ResponseEntity.status(HttpStatus.BAD_REQUEST).body(new ApiError("invalid_request")); }
    @ExceptionHandler(NotFound.class)
    public ResponseEntity<ApiError> missing() { return ResponseEntity.status(HttpStatus.NOT_FOUND).body(new ApiError("not_found")); }
    @ExceptionHandler(RejectedWrite.class)
    public ResponseEntity<ApiError> rejected() { return ResponseEntity.status(HttpStatus.CONFLICT).body(new ApiError("rejected_write")); }
}
